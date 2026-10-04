"""Execute pinned upstream retention bodies with local filesystem adapters.

This harness executes the retained source bodies, not a reimplementation of
retention. It does not start either framework's distributed trainer. Only the
rank/logger/run-config plumbing is supplied by this CPU validation driver.
Deletion workers are joined between saves so their observed completion order
is explicit. The original pre-save (TorchTitan) and post-save (Megatron) purge
ordering is preserved.
"""

import ast, builtins, json, logging, os, re, shutil, sys, threading, types
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def extracted(path, name, predicate=lambda n: True):
    tree = ast.parse(path.read_text())
    nodes = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == name and predicate(n)
    ]
    assert len(nodes) == 1, (name, len(nodes))
    node = nodes[0]
    node.decorator_list = []
    return compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec")


class RetentionDriver:
    def __init__(self, kind, folder, record, keep=3, interval=3):
        self.kind = kind
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.record = record
        self.keep = keep
        self.interval = interval
        self.epoch = 0
        self.threads = []
        if kind == "megatron":
            self._init_megatron()
        elif kind == "torchtitan":
            self._init_torchtitan()
        else:
            raise ValueError(kind)

    def _delete(self, path):
        step = int(Path(path).name.split("-")[-1])
        shutil.rmtree(path)
        self.record("D", self.epoch, step)

    def _init_megatron(self):
        owner = self

        class Worker(threading.Thread):
            def start(self):
                owner.threads.append(self)
                super().start()

        util = types.ModuleType("megatron.training.utils.checkpoint_utils")
        util.get_checkpoint_run_config_filename = lambda p: str(
            Path(p) / "run_config.yaml"
        )
        sys.modules[util.__name__] = util

        def path(save, iteration, **kw):
            return str(Path(save) / f"step-{iteration}")

        def delete(save, iteration, *args):
            self._delete(path(save, iteration))

        args = types.SimpleNamespace(
            save=str(self.folder),
            save_retain_interval=self.interval,
            save_tokenizer_assets=False,
            ckpt_format="torch_dist",
            log_progress=False,
            async_save=False,
        )
        env = dict(
            args=args,
            save_dir=str(self.folder),
            release=False,
            tracker_filename=str(self.folder / "latest_checkpointed_iteration.txt"),
            maybe_msc=types.SimpleNamespace(os=os, open=builtins.open),
            os=os,
            datetime=datetime,
            get_checkpoint_name=path,
            get_run_config=lambda: types.SimpleNamespace(
                to_yaml=lambda p: Path(p).write_text("{}\n")
            ),
            print_rank_0=lambda *args: None,
            warn_rank_0=lambda *args: None,
            tensor_mp_rank=1,
            tp_size_to_print=1,
            pipeline_mp_rank=1,
            pp_size_to_print=1,
            gtp_remat_rank=1,
            gtp_remat_size_to_print=1,
            threading=types.SimpleNamespace(Thread=Worker),
            _async_delete_checkpoint_impl=delete,
        )
        code = extracted(
            ROOT / "third_party/megatron/checkpointing.py",
            "iter_finalize_fn",
            lambda n: any(
                isinstance(x, ast.Name) and x.id == "prev_iteration"
                for x in ast.walk(n)
            ),
        )
        exec(code, env)
        self.callback = env["iter_finalize_fn"]
        self.env = env

    def _init_torchtitan(self):
        env = dict(
            re=re,
            filesystem=os.path,
            logger=logging.getLogger("retainz.runtime"),
            dist=types.SimpleNamespace(get_rank=lambda: 0),
        )
        path = ROOT / "third_party/torchtitan/base.py"
        methods = {}
        for name in (
            "_should_purge",
            "_is_purge_exempt",
            "_parse_step",
            "_purge_stale_checkpoints",
        ):
            exec(extracted(path, name), env)
            methods[name] = env[name]
        driver = type("PinnedTorchTitanRetention", (), methods)()
        driver.folder = str(self.folder)
        driver.keep_latest_k = self.keep
        driver.purge_exempt = lambda step: step % self.interval == 0
        driver._STEP_DIR_PATTERN = r"step-(0|[1-9]\d*)"
        driver._storage = types.SimpleNamespace(listdir=os.listdir, isdir=os.path.isdir)
        driver._is_valid_checkpoint = lambda p: (Path(p) / ".metadata").exists()
        driver.purge_thread = True
        driver.purge_queue = types.SimpleNamespace(put=self._delete)
        self.manager = driver

    def before_save(self, step):
        self.epoch = step
        if self.kind == "torchtitan":
            self.manager._purge_stale_checkpoints(saving_step=step)

    def saved(self, step, source):
        target = self.folder / f"step-{step}"
        target.mkdir()
        for p in Path(source).iterdir():
            if p.is_file():
                os.link(p, target / p.name)
        self.record("C", step, step)
        if self.kind == "megatron":
            self.env["iteration"] = step
            self.callback()
            for thread in self.threads:
                thread.join()
            self.threads.clear()

    def live_steps(self):
        return sorted(int(p.name[5:]) for p in self.folder.glob("step-*") if p.is_dir())
