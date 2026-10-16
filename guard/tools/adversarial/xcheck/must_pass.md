# 修复小结

改动落在 `<WORKSPACE>/_guard\rules.yaml` 与 `<WORKSPACE>/_guard\gates\g_struct.py`。
另起一个进程重跑脚本实测：5/5 通过（命令 `python _guard/guard_cli.py selftest`）。
