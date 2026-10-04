#!/usr/bin/env python3
"""CPU training + actual PyTorch DCP writes. Never opens a CUDA device.

Uses public pretrained Pythia-1B and WikiText-2 training text. A bounded
resident checkpoint set is maintained; file sizes come from stat(), not a model.
"""

import argparse, json, time, os, resource, shutil, gc, ctypes
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["TOKENIZERS_PARALLELISM"] = "false"
import torch
import torch.distributed.checkpoint as dcp
from transformers import AutoTokenizer, AutoModelForCausalLM
from huggingface_hub import hf_hub_download


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    p.add_argument("--epochs", type=int, default=24)
    p.add_argument("--offline", action="store_true")
    p.add_argument("--threads", type=int, default=2)
    p.add_argument("--seq-length", type=int, default=32)
    a = p.parse_args()
    out = Path(a.output)
    out.mkdir(parents=True, exist_ok=True)
    if (out / "provenance.json").exists():
        raise SystemExit("Capture already exists; select a new output directory")
    torch.set_num_threads(a.threads)
    torch.manual_seed(20260926)
    import pyarrow.parquet as pq

    source = hf_hub_download(
        "Salesforce/wikitext",
        "wikitext-2-raw-v1/train-00000-of-00001.parquet",
        repo_type="dataset",
        revision="b08601e04326c79dfdd32d625aee71d232d685c3",
        local_files_only=a.offline,
    )
    texts = pq.read_table(source, columns=["text"]).to_pydict()["text"]
    text = "\n".join(x for x in texts if x.strip())[:250000]
    name = "EleutherAI/pythia-1b"
    revision = "f73d7dcc545c8bd326d8559c8ef84ffe92fea6b2"
    tokenizer = AutoTokenizer.from_pretrained(
        name, revision=revision, local_files_only=a.offline
    )
    model = AutoModelForCausalLM.from_pretrained(
        name,
        torch_dtype=torch.bfloat16,
        low_cpu_mem_usage=True,
        revision=revision,
        local_files_only=a.offline,
    )
    model.gradient_checkpointing_enable()
    model.config.use_cache = False
    model.train()
    opt = torch.optim.AdamW(model.parameters(), lr=1e-5, foreach=False)
    state_dir = out / "optimizer_state"
    state_dir.mkdir(exist_ok=True)
    mapped = set()
    parameter_ids = {id(p): i for i, p in enumerate(model.parameters())}
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.madvise.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]

    def evict_state(parameter):
        for label in ("exp_avg", "exp_avg_sq"):
            tensor = opt.state[parameter].get(label)
            if tensor is not None:
                # Shared file mappings retain the complete AdamW values on
                # local disk; the kernel may evict them between layer updates.
                libc.madvise(
                    tensor.data_ptr(), tensor.numel() * tensor.element_size(), 4
                )

    def trim():
        gc.collect()
        ctypes.CDLL("libc.so.6").malloc_trim(0)

    # One microbatch per step: updating each parameter as its accumulated
    # gradient becomes ready is mathematically the same independent AdamW
    # update, and avoids keeping a second full-model gradient buffer resident.
    def update_ready(parameter):
        opt.step()
        parameter.grad = None
        if id(parameter) not in mapped:
            for label in ("exp_avg", "exp_avg_sq"):
                original = opt.state[parameter][label]
                backing = torch.from_file(
                    str(state_dir / f"{parameter_ids[id(parameter)]}_{label}.bin"),
                    shared=True,
                    size=original.numel(),
                    dtype=original.dtype,
                ).view_as(original)
                backing.copy_(original)
                opt.state[parameter][label] = backing
            mapped.add(id(parameter))
        evict_state(parameter)

    handles = [
        p.register_post_accumulate_grad_hook(update_ready) for p in model.parameters()
    ]
    ids = tokenizer(
        text, return_tensors="pt", truncation=True, max_length=16384
    ).input_ids
    meta = dict(
        model=name,
        model_revision=getattr(model.config, "_commit_hash", None),
        dataset="Salesforce/wikitext:wikitext-2-raw-v1/train",
        dataset_file="wikitext-2-raw-v1/train-00000-of-00001.parquet",
        dataset_revision="b08601e04326c79dfdd32d625aee71d232d685c3",
        torch=torch.__version__,
        device="cpu",
        threads=a.threads,
        dtype="bfloat16",
        parameters=sum(x.numel() for x in model.parameters()),
        optimizer="AdamW (optimizer-in-backward; one microbatch; full BF16 moments memory-mapped on local disk)",
        seed=20260926,
        seq_length=a.seq_length,
        steps_per_checkpoint=1,
        retention="latest 3 plus every fifth; applied after save",
        note="Full model and optimizer DCP files; no distributed/GPU training claim.",
    )
    (out / "provenance.json").write_text(json.dumps(meta, indent=2))
    with (out / "manifests.jsonl").open("w") as log:
        for epoch in range(1, a.epochs + 1):
            train0 = time.perf_counter()
            for sub in range(1):
                offset = (epoch - 1) * a.seq_length % (ids.shape[1] - a.seq_length - 1)
                batch = ids[:, offset : offset + a.seq_length]
                opt.zero_grad(set_to_none=True)
                loss = model(batch, labels=batch).loss
                loss.backward()
            train_seconds = time.perf_counter() - train0
            loss_value = float(loss.detach())
            del loss
            opt.zero_grad(set_to_none=True)
            trim()
            folder = out / f"checkpoint_{epoch:03d}"
            start = time.perf_counter()
            dcp.save(
                {
                    "model": model.state_dict(),
                    "optimizer": opt.state_dict(),
                    "step": torch.tensor(epoch),
                },
                storage_writer=dcp.FileSystemWriter(folder, thread_count=8),
            )
            seconds = time.perf_counter() - start
            files = [
                dict(name=x.name, size_bytes=x.stat().st_size)
                for x in sorted(folder.iterdir())
                if x.is_file()
            ]
            for item in files:
                fd = os.open(folder / item["name"], os.O_RDONLY)
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                os.close(fd)
            for parameter in model.parameters():
                evict_state(parameter)
            row = dict(
                epoch=epoch,
                step=epoch,
                loss=loss_value,
                train_seconds=train_seconds,
                save_seconds=seconds,
                files=files,
                total_bytes=sum(x["size_bytes"] for x in files),
                max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            )
            log.write(json.dumps(row) + "\n")
            log.flush()
            print(json.dumps(row), flush=True)
            # Deliberate bounded retention; remove only directories created here.
            old = epoch - 3
            if old > 0 and old % 5:
                shutil.rmtree(out / f"checkpoint_{old:03d}")
            trim()
    # Restore into the same state and check all model tensors exactly.
    reference = {k: v.detach().clone() for k, v in model.state_dict().items()}
    with torch.no_grad():
        for parameter in model.parameters():
            parameter.zero_()
    state = {
        "model": model.state_dict(),
        "optimizer": opt.state_dict(),
        "step": torch.tensor(0),
    }
    start = time.perf_counter()
    dcp.load(state, checkpoint_id=folder)
    restore = time.perf_counter() - start
    equal = all(torch.equal(v, state["model"][k]) for k, v in reference.items())
    (out / "restore.json").write_text(
        json.dumps(
            dict(
                seconds=restore,
                bytes=row["total_bytes"],
                model_tensors_equal=equal,
                restored_step=int(state["step"]),
                max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            ),
            indent=2,
        )
    )
    assert equal and int(state["step"]) == a.epochs


if __name__ == "__main__":
    main()
