#!/usr/bin/env python3
"""上桌吃饭 — AI可玩版引擎

接口（跟钓鱼游戏一样）:
  new_game(seed)    → (state, text)   开新局
  cmd(state, inst)  → (state, text)   执行指令
  load_game()       → state | None    从文件读
  save_game(state)  → None            存文件

AI接入方式:
  1. 函数调用: import engine; state = engine.new_game()[0]; state, text = engine.cmd(state, "菜场")
  2. 命令行:   python engine.py "菜场"  (自动读存档、执行、存回)
  3. HTTP API: python engine.py --serve  (Flask, port 8877)

特性:
  - 批量指令: "买 番茄 2;买 鸡蛋 1" 分号串联
  - 状态栏JSON: 每次输出末尾带紧凑状态
  - 确定性PRNG: 同seed同指令=同结果
  - 半斤支持: "买 五花肉 0.5" 或 "买 五花肉 半斤"
"""

import sys, os, io, json, time

# 确保UTF-8输出
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

try:
    _HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _HERE = os.getcwd()
_SAVE_FILE = os.path.join(_HERE, "market_engine_save.json")

# ── 确定性PRNG ──────────────────────────────────────
def mulberry32(seed):
    """确定性随机数生成器。同seed=同序列。"""
    def _gen():
        nonlocal seed
        while True:
            seed = (seed + 0x6D2B79F5) & 0xFFFFFFFF
            t = seed
            t = ((t ^ (t >> 15)) * (t | 1)) & 0xFFFFFFFF
            t = ((t ^ (t >> 15)) * (t | 1)) & 0xFFFFFFFF
            yield (t ^ (t >> 15)) & 0xFFFFFFFF
    return _gen()


class _DetRandom:
    """替换random的确定性随机。"""
    def __init__(self, seed=42):
        self._gen = mulberry32(seed)
        self._state = seed

    def seed(self, s):
        self._state = s
        self._gen = mulberry32(s)

    def random(self):
        return next(self._gen) / 0xFFFFFFFF

    def randint(self, a, b):
        return a + int(self.random() * (b - a + 1))

    def choice(self, seq):
        return seq[self.randint(0, len(seq) - 1)]

    def sample(self, seq, k):
        idxs = list(range(len(seq)))
        result = []
        for _ in range(min(k, len(idxs))):
            i = self.randint(0, len(idxs) - 1)
            result.append(seq[idxs[i]])
            idxs.pop(i)
        return result

    def shuffle(self, seq):
        for i in range(len(seq) - 1, 0, -1):
            j = self.randint(0, i)
            seq[i], seq[j] = seq[j], seq[i]

    def choices(self, population, weights=None, k=1):
        if weights is None:
            return [self.choice(population) for _ in range(k)]
        total = sum(weights)
        result = []
        for _ in range(k):
            r = self.random() * total
            cum = 0
            for item, w in zip(population, weights):
                cum += w
                if r <= cum:
                    result.append(item)
                    break
            else:
                result.append(population[-1])
        return result


# ── 注入确定性随机 ─────────────────────────────────
import random as _stdlib_random
_det_rng = _DetRandom(42)


def _patch_random():
    """把market_engine的random换成确定性版本。"""
    import market_engine
    market_engine.random = _det_rng


# ── 快照 ────────────────────────────────────────────
_SKIP_ATTRS = {'kitchen_state'}  # kitchen_state单独处理（含set）

def _to_jsonable(obj):
    """递归把不可JSON序列化的东西转成可序列化的。"""
    if isinstance(obj, set):
        return {'__t': 'set', 'v': sorted(obj, key=str)}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, float):
        return round(obj, 4)  # 避免浮点精度问题
    return obj


def _from_jsonable(obj):
    """反序列化。"""
    if isinstance(obj, dict):
        if obj.get('__t') == 'set':
            return set(_from_jsonable(x) for x in obj.get('v', []))
        return {k: _from_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_jsonable(x) for x in obj]
    return obj


def _snapshot(game):
    """把MarketGame所有可序列化属性拍成dict。"""
    state = {}
    for attr in dir(game):
        if attr.startswith('__') or attr in _SKIP_ATTRS:
            continue
        val = getattr(game, attr, None)
        if callable(val):
            continue
        try:
            state[attr] = _to_jsonable(val)
        except:
            pass
    # kitchen_state单独处理——set→list
    if game.kitchen_state:
        state['_kitchen_state'] = _to_jsonable(game.kitchen_state)
    else:
        state['_kitchen_state'] = None
    # PRNG状态
    state['_rng_state'] = _det_rng._state
    # MarketGame内部rng闭包的当前seed
    try:
        state['_game_rng_seed'] = game.rng.__closure__[0].cell_contents
    except (AttributeError, IndexError):
        state['_game_rng_seed'] = game.seed
    return state


def _restore(game, state):
    """从快照恢复。"""
    rng_state = state.pop('_rng_state', None)
    if rng_state is not None:
        _det_rng.seed(rng_state)

    # 恢复MarketGame内部rng闭包
    game_rng_seed = state.pop('_game_rng_seed', None)
    if game_rng_seed is not None:
        from market_engine import mulberry32
        game.rng = mulberry32(game_rng_seed)

    kitchen_data = state.pop('_kitchen_state', None)
    # 从state里去掉纯缓存属性（可以lazy重建）
    for attr in ('_yield_cache', '_stall_item_cache'):
        state.pop(attr, None)
    # 以下属性在同一天内跨cmd()必须保持，不pop
    # _owner_daily, _today_disaster, _disaster_price_mod, _disaster_quality_mod,
    # _disaster_bargain_bonus, _season_stall_items, _rare_boost_today,
    # _neighbor_conflict, _roof_leaking, _journey_text, _journey_shown

    for attr, val in state.items():
        if attr in _SKIP_ATTRS:
            continue
        try:
            setattr(game, attr, _from_jsonable(val))
        except:
            pass
    # 恢复kitchen_state——list→set
    if kitchen_data:
        game.kitchen_state = _restore_sets(_from_jsonable(kitchen_data))
    else:
        game.kitchen_state = None


def _restore_sets(obj):
    """kitchen_state里的set字段还原。处理两种格式：list和{'__t':'set','v':list}。"""
    _SET_KEYS = {"completed_steps", "completed_optional", "_on_board", "discovered_recipes"}
    if isinstance(obj, dict):
        if obj.get('__t') == 'set':
            return set(obj.get('v', []))
        result = {}
        for k, v in obj.items():
            if k in _SET_KEYS:
                if isinstance(v, list):
                    result[k] = set(v)
                elif isinstance(v, dict) and v.get('__t') == 'set':
                    result[k] = set(v.get('v', []))
                else:
                    result[k] = _restore_sets(v)
            else:
                result[k] = _restore_sets(v)
        return result
    if isinstance(obj, list):
        return [_restore_sets(x) if isinstance(x, (dict, list)) else x for x in obj]
    return obj


# ── 状态栏 ──────────────────────────────────────────
def _status_bar(game):
    """紧凑JSON状态栏——让AI知道在哪。"""
    bar = {
        "day": game.day,
        "season": game.season,
        "weather": game.weather,
    }
    if game.kitchen_state is not None:
        bar["phase"] = "厨房"
        if game.kitchen_state.get("dish_name"):
            bar["dish"] = game.kitchen_state["dish_name"]
    elif game.basket:
        bar["phase"] = "买菜"
    else:
        bar["phase"] = "菜场"

    if not game.done:
        bar["budget"] = f"{game.budget - game.spent:.1f}/{game.budget}"
        bar["basket"] = len(game.basket)
        bar["time"] = f"{game.market_time}/{game.market_time_max}"
    else:
        bar["phase"] = "吃完"

    return json.dumps(bar, ensure_ascii=False, separators=(',', ':'))


# ── 核心接口 ────────────────────────────────────────
_initialized = False

def _ensure_init():
    global _initialized
    if not _initialized:
        sys.path.insert(0, _HERE)
        _patch_random()
        _initialized = True


def new_game(seed=None):
    """开新局。返回 (state_dict, 开场文字)。"""
    _ensure_init()
    from market_engine import MarketGame
    if seed is not None:
        _det_rng.seed(seed)
    game = MarketGame()
    # new_day()会load旧存档再重置——但我们要干净的新局
    game.seed = seed if seed is not None else int(time.time()) & 0xFFFFFFFF
    text = game.new_day(seed=game.seed)
    state = _snapshot(game)
    return state, text


def cmd(state, instruction):
    """执行指令。返回 (新state, 输出文字)。

    支持分号串联:
      "买 番茄;买 鸡蛋 2"  → 依次执行
    """
    _ensure_init()
    from market_engine import MarketGame

    # 恢复游戏
    game = MarketGame()
    _restore(game, state)

    # 处理分号串联
    if ';' in instruction:
        parts = [p.strip() for p in instruction.split(';') if p.strip()]
        texts = []
        for part in parts[:8]:
            texts.append(game.cmd(part))
            if game.done:
                break
        full_text = "\n---\n".join(texts)
    else:
        full_text = game.cmd(instruction)

    # 快照新状态
    new_state = _snapshot(game)
    status = _status_bar(game)
    output = full_text + "\n" + status
    return new_state, output


def load_game():
    """从文件读存档。返回 state_dict 或 None。"""
    if not os.path.exists(_SAVE_FILE):
        return None
    try:
        with open(_SAVE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return None


def save_game(state):
    """存档到文件。"""
    try:
        with open(_SAVE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, separators=(',', ':'))
    except:
        pass


# ── 命令行入口 ──────────────────────────────────────
def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("用法: python engine.py \"指令\"")
        print("  python engine.py new          — 开新局")
        print("  python engine.py 菜场          — 继续游戏")
        print("  python engine.py 买 番茄;买 鸡蛋 — 串联指令")
        print("  python engine.py --serve       — 启动HTTP API")
        return

    arg = sys.argv[1]

    if arg == "--serve":
        _serve()
        return

    instruction = " ".join(sys.argv[1:]).strip()

    if instruction.lower() in ("new", "新局", "new_game"):
        state, text = new_game()
        save_game(state)
        print(text)
        return

    # 读存档
    state = load_game()
    if state is None:
        state, text = new_game()
        save_game(state)
        print(text)
        print("\n（自动开新局。输入 python engine.py \"菜场\" 开始。）")
        return

    # 执行
    new_state, text = cmd(state, instruction)
    save_game(new_state)
    print(text)


# ── HTTP API ────────────────────────────────────────
def _serve():
    """启动Flask HTTP API，给任何AI玩。"""
    try:
        from flask import Flask, jsonify, request
    except ImportError:
        print("需要Flask: pip install flask")
        return

    app = Flask(__name__)
    import threading
    _lock = threading.Lock()
    _games = {}  # session_id → state

    @app.route("/")
    def index():
        return jsonify({
            "game": "上桌吃饭",
            "endpoints": {
                "POST /new": "开新局 (可选 ?seed=123)",
                "POST /cmd": "执行指令 (body: {session, instruction})",
                "GET /state": "查看状态 (query: ?session=xxx)",
            }
        })

    @app.route("/new", methods=["POST"])
    def new():
        with _lock:
            seed = request.args.get("seed", type=int)
            state, text = new_game(seed)
            sid = str(int(time.time() * 1000))
            _games[sid] = state
            # 只保留最近10个session
            while len(_games) > 10:
                oldest = next(iter(_games))
                del _games[oldest]
            return jsonify({"session": sid, "text": text})

    @app.route("/cmd", methods=["POST"])
    def do_cmd():
        body = request.get_json(silent=True) or {}
        sid = body.get("session", "")
        inst = body.get("instruction", "").strip()
        if not sid or sid not in _games:
            return jsonify({"error": "无效session，先POST /new"}), 400
        if not inst:
            return jsonify({"error": "空指令"}), 400
        with _lock:
            state = _games[sid]
            new_state, text = cmd(state, inst)
            _games[sid] = new_state
            return jsonify({"text": text})

    @app.route("/state", methods=["GET"])
    def get_state():
        sid = request.args.get("session", "")
        if not sid or sid not in _games:
            return jsonify({"error": "无效session"}), 400
        return jsonify(_games[sid])

    port = int(os.environ.get("MARKET_PORT", 8877))
    print(f"上桌吃饭 HTTP API — localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
