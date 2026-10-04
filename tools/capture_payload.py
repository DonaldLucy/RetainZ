#!/usr/bin/env python3
"""Capture real model bytes and execute pinned retention callbacks on their files."""

import argparse, json, os, resource, sys, time
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["TOKENIZERS_PARALLELISM"] = "false"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime_retention import RetentionDriver


def evict(path):
    fd = os.open(path, os.O_RDONLY)
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    os.close(fd)


def main():
    import torch
    import torch.distributed.checkpoint as dcp
    from transformers import AutoTokenizer, AutoModelForCausalLM
    from huggingface_hub import hf_hub_download
    import pyarrow.parquet as pq

    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, default=ROOT / "build/checkpoints")
    p.add_argument("--epochs", type=int, default=8)
    p.add_argument("--offline", action="store_true")
    a = p.parse_args()
    out = a.output
    out.mkdir(parents=True, exist_ok=True)
    if (out / "capture.json").exists():
        raise SystemExit("Capture already exists; preserve measured inputs")
    torch.set_num_threads(2)
    torch.manual_seed(20260927)
    torch.use_deterministic_algorithms(True)
    name = "HuggingFaceTB/SmolLM2-135M"
    revision = "93efa2f097d58c2a74874c7e644dbc9b0cee75a2"
    dataset_revision = "b08601e04326c79dfdd32d625aee71d232d685c3"
    tokenizer = AutoTokenizer.from_pretrained(
        name, revision=revision, local_files_only=a.offline
    )
    model = AutoModelForCausalLM.from_pretrained(
        name, torch_dtype=torch.bfloat16, revision=revision, local_files_only=a.offline
    )
    model.train()
    opt = torch.optim.AdamW(model.parameters(), lr=1e-5, foreach=False)
    source = hf_hub_download(
        "Salesforce/wikitext",
        "wikitext-2-raw-v1/train-00000-of-00001.parquet",
        repo_type="dataset",
        revision=dataset_revision,
        local_files_only=a.offline,
    )
    texts = pq.read_table(source, columns=["text"]).to_pydict()["text"]
    text = "\n".join(x for x in texts if x.strip())[:100000]
    ids = tokenizer(
        text, return_tensors="pt", truncation=True, max_length=4096
    ).input_ids
    torch.save(ids, out / "token_ids.pt")
    events = {"megatron": [], "torchtitan": []}
    drivers = {
        kind: RetentionDriver(
            kind,
            out / ("retention_" + kind),
            lambda op, epoch, step, k=kind: events[k].append(
                dict(op=op, epoch=epoch, step=step)
            ),
        )
        for kind in events
    }
    records = []
    index = []
    cursor = 0
    with (out / "payload.img").open("wb") as image:
        for step in range(1, a.epochs + 1):
            for driver in drivers.values():
                driver.before_save(step)
            batch = ids[:, (step - 1) * 32 : step * 32]
            t = time.perf_counter()
            opt.zero_grad(set_to_none=True)
            loss = model(batch, labels=batch).loss
            loss.backward()
            opt.step()
            train_s = time.perf_counter() - t
            folder = out / "checkpoints" / f"step-{step}"
            t = time.perf_counter()
            dcp.save(
                dict(
                    model=model.state_dict(),
                    optimizer=opt.state_dict(),
                    step=torch.tensor(step),
                    rng=torch.get_rng_state(),
                ),
                storage_writer=dcp.FileSystemWriter(folder, thread_count=4),
            )
            save_s = time.perf_counter() - t
            files = []
            start = cursor
            for path in sorted(folder.iterdir()):
                if not path.is_file():
                    continue
                size = path.stat().st_size
                files.append(dict(name=path.name, size_bytes=size, image_offset=cursor))
                with path.open("rb") as inp:
                    while chunk := inp.read(1024 * 1024):
                        image.write(chunk)
                padding = (-size) % 4096
                image.write(bytes(padding))
                cursor += size + padding
                evict(path)
            index.append(f"rt{step} {start}")
            records.append(
                dict(
                    step=step,
                    total_bytes=sum(f["size_bytes"] for f in files),
                    stored_bytes=cursor - start,
                    image_offset=start,
                    files=files,
                    loss=float(loss.detach()),
                    train_seconds=train_s,
                    save_seconds=save_s,
                )
            )
            for driver in drivers.values():
                driver.saved(step, folder)
            print(
                json.dumps(
                    dict(
                        step=step,
                        bytes=records[-1]["total_bytes"],
                        train_seconds=train_s,
                        save_seconds=save_s,
                    )
                ),
                flush=True,
            )
        image.flush()
        os.fsync(image.fileno())
    evict(out / "payload.img")
    (out / "payload.index").write_text("\n".join(index) + "\n")
    for kind, driver in drivers.items():
        lines = []
        for e in events[kind]:
            step = e["step"]
            epoch = e["epoch"]
            key = f"rt{step}"
            if e["op"] == "D":
                lines.append(f"D {epoch} {key}")
                continue
            row = records[step - 1]
            death = (
                "inf" if step % 3 == 0 else str(step + (1 if kind == "megatron" else 3))
            )
            cl = "extreme" if step % 3 == 0 else "short"
            lines.append(
                f'C {epoch} {key} {death} {cl} {row["total_bytes"]} '
                + ",".join(str(f["size_bytes"]) for f in row["files"])
            )
            e["observed_files"] = len(row["files"])
        (out / (kind + ".tsv")).write_text("\n".join(lines + ["Q"]) + "\n")
        (out / (kind + "_restore.tsv")).write_text(
            "".join(f"R {a.epochs+1} rt{step}\n" for step in driver.live_steps())
            + "Q\n"
        )
        (out / (kind + "_events.json")).write_text(
            json.dumps(
                dict(
                    events=events[kind],
                    live_steps=driver.live_steps(),
                    ordering=(
                        "post-save completion"
                        if kind == "megatron"
                        else "pre-save purge"
                    ),
                    scope="Unmodified upstream retention callback bodies; local rank-0 plumbing and joined deletion workers; DCP trainer is this driver, not the full distributed framework.",
                ),
                indent=2,
            )
            + "\n"
        )
    meta = dict(
        model=name,
        revision=getattr(model.config, "_commit_hash", None),
        dataset="WikiText-2",
        dataset_file="wikitext-2-raw-v1/train-00000-of-00001.parquet",
        dataset_revision=dataset_revision,
        device="CPU",
        dtype="BF16",
        epochs=a.epochs,
        threads=2,
        torch=torch.__version__,
        parameters=sum(x.numel() for x in model.parameters()),
        optimizer="AdamW",
        seed=20260927,
        sequence_length=32,
        steps_per_checkpoint=1,
        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        payload_bytes=cursor,
        records=records,
    )
    (out / "capture.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps({k: v for k, v in meta.items() if k != "records"}), flush=True)


if __name__ == "__main__":
    main()
