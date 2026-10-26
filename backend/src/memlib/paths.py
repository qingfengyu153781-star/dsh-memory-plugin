"""代码根 / 数据根的唯一解析点。改路径只改这里。

为什么要把两个根分开（2026-10-26 实测病根）：
  旧写法 `ROOT = dirname(dirname(dirname(abspath(__file__))))` 一个变量兼任两职 ——
  既当 `sys.path` 的代码根，又当 `raw/ index/ reports/` 的数据根。
  后果：**代码装到哪，数据就必须跟着放哪**。于是
    · 装进 `node_modules/<插件>/backend/` 时，数据会被写进包管理器目录（升级即丢）；
    · 而一个外人 clone 下来，数据根落在他的代码目录里，与本机 5.1G 库毫无关系。
  两个根分开之后：代码可以来自安装包，数据永远在用户自己的地方。

解析顺序（数据根）：
  1. `DSH_MEMORY_DATA`          —— 显式指定，最高优先级（插件就是用这个传给子进程的）
  2. 从代码根上溯，找"像记忆库根"的目录（含 `index/` 或 `raw/`）
     —— 向后兼容本机既有数据：`<库>/src/memlib/paths.py` 与
        `<库>/plugin-src/dsh-memory-plugin/backend/src/memlib/paths.py` 都能上溯到 `<库>`
  3. `~/.dsh/memory-library`    —— 全新安装的每用户默认位置

★ 上溯**不越过 `node_modules`**：包内安装时不许往上爬进别人的目录去认领数据。
★ 一律用 `abspath` 不用 `resolve()`：pnpm 的 `node_modules/<pkg>` 是符号链接，
  `resolve()` 会指进 `.pnpm/<hash>/` 内容寻址仓库（只读、会被 prune 清掉）。
"""
import os

#: 代码根。只用于 `sys.path` 与"找同仓数据"，**不再直接当数据根**。
#: backend/src/memlib/paths.py -> backend/ ；记忆库仓 src/memlib/paths.py -> 仓根
CODE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 判定"这个目录像不像记忆库根"的签名目录
_SIGNS = ("index", "raw")

#: 上溯层数上限。本机实测需要 4 层（backend → 插件 → plugin-src → 库根）
_MAX_UP = 5


def _has_data(d):
    """目录里有没有记忆库的数据签名。"""
    for s in _SIGNS:
        try:
            if os.path.isdir(os.path.join(d, s)):
                return True
        except OSError:
            pass
    return False


def _inside_node_modules(d):
    """判断某个路径是不是在包管理器目录里（含 `node_modules` 或 `.pnpm` 段）。"""
    parts = os.path.normcase(os.path.abspath(d)).replace("/", os.sep).split(os.sep)
    return "node_modules" in parts or ".pnpm" in parts


def resolve_data_root():
    """返回 `(data_root, source)`。`source` 用于自证：env / sibling-of-code / default。"""
    env = os.environ.get("DSH_MEMORY_DATA")
    if env and env.strip():
        return os.path.abspath(env.strip()), "env"

    cur = CODE_ROOT
    for _ in range(_MAX_UP):
        if _inside_node_modules(cur):
            break                      # 包内安装：不许往外认领别人的数据
        if _has_data(cur):
            return cur, "sibling-of-code"
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

    home = os.path.expanduser("~")
    return os.path.join(home, ".dsh", "memory-library"), "default"


DATA_ROOT, DATA_ROOT_SOURCE = resolve_data_root()

RAW = os.path.join(DATA_ROOT, "raw")
INDEX_DIR = os.path.join(DATA_ROOT, "index")
DB_PATH = os.path.join(INDEX_DIR, "memory.sqlite")
REPORT_DIR = os.path.join(DATA_ROOT, "reports")


def ensure_dirs():
    """建出数据根下必须存在的目录。调用方可以放心写。"""
    for d in (DATA_ROOT, RAW, INDEX_DIR, REPORT_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


def describe():
    """给 `mem.py paths` 用：把两个根与来源一次打全，便于排查。"""
    return {
        "codeRoot": CODE_ROOT,
        "dataRoot": DATA_ROOT,
        "dataRootSource": DATA_ROOT_SOURCE,
        "raw": RAW,
        "indexDir": INDEX_DIR,
        "dbPath": DB_PATH,
        "reportDir": REPORT_DIR,
        "dbExists": os.path.isfile(DB_PATH),
        "insideNodeModules": _inside_node_modules(CODE_ROOT),
    }
