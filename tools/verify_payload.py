#!/usr/bin/env python3
"""Verify exported FEMU bytes, restore DCP state, then compare one resumed step."""

import argparse, json, os, resource, time
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["TOKENIZERS_PARALLELISM"] = "false"
ROOT = Path(__file__).resolve().parents[1]


def main():
    import torch
    import torch.distributed.checkpoint as dcp
    from transformers import AutoModelForCausalLM, AutoConfig

    p = argparse.ArgumentParser()
    p.add_argument("case")
    p.add_argument("--input", type=Path, default=ROOT / "results/runs")
    p.add_argument("--payload-dir", type=Path, default=ROOT / "build/checkpoints")
    a = p.parse_args()
    out = a.input / a.case
    capture_dir = a.payload_dir
    capture = json.loads((capture_dir / "capture.json").read_text())
    run = json.loads((out / "run.json").read_text())
    assert run["guest_success"], "FEMU payload trial did not complete"
    runtime = run["case"]["runtime"]
    events = json.loads((capture_dir / (runtime + "_events.json")).read_text())
    restored = out / "dcp_restored"
    restored.mkdir(exist_ok=True)
    total = 0
    with (out / "restored.img").open("rb") as image:
        for step in events["live_steps"]:
            record = capture["records"][step - 1]
            folder = restored / f"step-{step}"
            folder.mkdir(exist_ok=True)
            for f in record["files"]:
                source = capture_dir / "checkpoints" / f"step-{step}" / f["name"]
                image.seek(f["image_offset"])
                left = f["size_bytes"]
                with source.open("rb") as original, (folder / f["name"]).open(
                    "wb"
                ) as exported:
                    while left:
                        n = min(left, 1024 * 1024)
                        data = image.read(n)
                        assert len(data) == n and data == original.read(n), (
                            step,
                            f["name"],
                            left,
                        )
                        exported.write(data)
                        left -= n
                        total += n
    torch.set_num_threads(2)
    torch.use_deterministic_algorithms(True)
    ids = torch.load(capture_dir / "token_ids.pt", weights_only=True)
    config = AutoConfig.from_pretrained(
        capture["model"], revision=capture["revision"], local_files_only=True
    )

    def load_checkpoint(folder):
        torch.manual_seed(capture["seed"])
        model = AutoModelForCausalLM.from_config(config).to(torch.bfloat16)
        model.train()
        opt = torch.optim.AdamW(model.parameters(), lr=1e-5, foreach=False)
        batch = ids[:, :32]
        model(batch, labels=batch).loss.backward()
        opt.step()
        opt.zero_grad(set_to_none=True)
        with torch.no_grad():
            for x in model.state_dict().values():
                x.zero_()
            for state in opt.state.values():
                for x in state.values():
                    if torch.is_tensor(x):
                        x.zero_()
        state = dict(
            model=model.state_dict(),
            optimizer=opt.state_dict(),
            step=torch.tensor(0),
            rng=torch.get_rng_state(),
        )
        dcp.load(state, checkpoint_id=folder)
        model.load_state_dict(state["model"])
        opt.load_state_dict(state["optimizer"])
        return model, opt, state

    def equal(a, b):
        if torch.is_tensor(a):
            return torch.equal(a, b)
        if isinstance(a, dict):
            return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
        if isinstance(a, (list, tuple)):
            return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
        return a == b

    step = max(events["live_steps"])
    start = time.perf_counter()
    ref, refopt, refstate = load_checkpoint(
        capture_dir / "checkpoints" / f"step-{step}"
    )
    model, opt, state = load_checkpoint(restored / f"step-{step}")
    load_s = time.perf_counter() - start
    assert equal(refstate, state), "DCP model, Adam, step or RNG differs"
    batch = ids[:, step * 32 : (step + 1) * 32]

    def advance(model, opt, rng):
        torch.set_rng_state(rng)
        opt.zero_grad(set_to_none=True)
        loss = model(batch, labels=batch).loss
        loss.backward()
        opt.step()
        return float(loss.detach())

    ref_loss = advance(ref, refopt, refstate["rng"])
    loss = advance(model, opt, state["rng"])
    assert (
        ref_loss == loss
        and equal(ref.state_dict(), model.state_dict())
        and equal(refopt.state_dict(), opt.state_dict())
    ), "Resumed update differs"
    result = dict(
        case=a.case,
        runtime=runtime,
        policy=run["case"]["policy"],
        live_steps=events["live_steps"],
        bytewise_equal=True,
        verified_original_bytes=total,
        restored_step=int(state["step"]),
        model_tensors_equal=True,
        optimizer_tensors_equal=True,
        rng_equal=True,
        next_step_loss_equal=True,
        next_step_parameters_equal=True,
        next_step_optimizer_equal=True,
        next_step_loss=loss,
        model_tensor_count=len(state["model"]),
        adam_parameter_states=len(state["optimizer"]["state"]),
        comparison_load_seconds=load_s,
        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        scope="Actual CPU-trained DCP files through FEMU direct I/O, fresh backend process journal recovery, exported bytes, zero-before-load DCP restore and resumed CPU update. Not end-to-end distributed GPU training.",
    )
    (out / "tensor_verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
