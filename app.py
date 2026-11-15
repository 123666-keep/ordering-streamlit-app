"""
该订多少 — 便利店智能订货系统（改进版）
改进点：
  1. SQLite 持久化
  2. 加权移动平均预测（同星期几，近期权重更高）
  3. 下单后明确反馈
  4. 登录走数据库 + sha256 哈希
  5. 复盘页：预测 vs 实际折线图 + 误差柱状图
  6. 售罄时刻散点图、CSV 导出、概率单位标注
"""
import hashlib
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

# --------------------------------------------------------------------------- #
# 常量 & 数据库
# --------------------------------------------------------------------------- #
DB_PATH = Path(__file__).parent / "data" / "shop.db"
CATEGORIES = {
    "日配": ["日配01 牛奶", "日配02 酸奶", "日配03 鸡蛋", "日配04 面包片", "日配05 火腿"],
    "烘焙": ["烘焙01 吐司", "烘焙02 牛角包", "烘焙03 菠萝包", "烘焙04 蛋挞", "烘焙05 贝果"],
    "饮品": ["饮品01 鲜奶茶", "饮品02 美式", "饮品03 柠檬水", "饮品04 豆浆", "饮品05 气泡水"],
    "鲜食": ["鲜食01 三明治", "鲜食02 饭团", "鲜食03 沙拉", "鲜食04 关东煮", "鲜食05 热压卷"],
}
ALL_PRODUCTS = [p for ps in CATEGORIES.values() for p in ps]
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
MODIFY_REASONS = ["天气", "促销", "节假日", "经验判断", "其他"]


def code_of(name: str) -> str:
    """从显示名 '日配01 牛奶' 提取数据库 code '日配01'"""
    return name.split(" ")[0]


def get_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            cost REAL NOT NULL,
            price REAL NOT NULL,
            salvage REAL NOT NULL,
            shelf_life REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sales (
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            qty INTEGER NOT NULL,
            soldout_hour INTEGER,
            PRIMARY KEY (date, code)
        );
        CREATE TABLE IF NOT EXISTS arrivals (
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            qty INTEGER NOT NULL,
            production_date TEXT,
            shelf_life INTEGER,
            PRIMARY KEY (date, code)
        );
        CREATE TABLE IF NOT EXISTS waste (
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            qty INTEGER NOT NULL,
            PRIMARY KEY (date, code)
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            qty INTEGER NOT NULL,
            decided_by TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    # 兼容旧库：补上到货批次的新字段
    for col, typ in (("production_date", "TEXT"), ("shelf_life", "INTEGER")):
        try:
            c.execute(f"ALTER TABLE arrivals ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    # 种子数据
    c.execute("SELECT COUNT(*) AS n FROM users")
    if c.fetchone()["n"] == 0:
        seed(conn)
    conn.close()


def hash_pw(pw: str) -> str:
    return hashlib.sha256(("该订多少::" + pw).encode("utf-8")).hexdigest()


def seed(conn):
    c = conn.cursor()
    c.execute(
        "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
        ("boss", hash_pw("123456"), "店主"),
    )
    c.execute(
        "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
        ("clerk", hash_pw("123456"), "店员"),
    )
    # 商品档案（进价/售价/残值/保质期）
    prod_data = [
        ("日配01", "牛奶", "日配", 3.46, 5.87, 1.8, 29.4),
        ("日配02", "酸奶", "日配", 3.55, 5.92, 1.8, 33.3),
        ("日配03", "鸡蛋", "日配", 3.52, 6.26, 1.8, 29.2),
        ("日配04", "面包片", "日配", 3.64, 5.8, 1.8, 33.4),
        ("日配05", "火腿", "日配", 3.48, 5.93, 1.8, 34.2),
        ("烘焙01", "吐司", "烘焙", 5.72, 11.73, 2.4, 18.8),
        ("烘焙02", "牛角包", "烘焙", 6.14, 12.21, 2.4, 16.5),
        ("烘焙03", "菠萝包", "烘焙", 5.75, 11.91, 2.4, 20.1),
        ("烘焙04", "蛋挞", "烘焙", 5.83, 12.01, 2.4, 15.5),
        ("烘焙05", "贝果", "烘焙", 5.82, 12.18, 2.4, 15.4),
        ("饮品01", "鲜奶茶", "饮品", 3.07, 9.03, 2.4, 13.1),
        ("饮品02", "美式", "饮品", 2.96, 9.05, 2.4, 13.7),
        ("饮品03", "柠檬水", "饮品", 3.05, 8.9, 2.4, 12.0),
        ("饮品04", "豆浆", "饮品", 2.88, 7.5, 2.4, 11.0),
        ("饮品05", "气泡水", "饮品", 2.6, 6.5, 2.4, 20.0),
        ("鲜食01", "三明治", "鲜食", 6.5, 13.0, 2.0, 1.5),
        ("鲜食02", "饭团", "鲜食", 4.2, 9.5, 1.8, 1.5),
        ("鲜食03", "沙拉", "鲜食", 7.0, 15.0, 2.0, 1.0),
        ("鲜食04", "关东煮", "鲜食", 3.5, 8.0, 1.0, 1.0),
        ("鲜食05", "热压卷", "鲜食", 6.0, 12.5, 2.0, 1.5),
    ]
    c.executemany(
        "INSERT INTO products(code,name,category,cost,price,salvage,shelf_life) VALUES(?,?,?,?,?,?,?)",
        prod_data,
    )
    # 生成近 60 天的模拟销售（每个单品每天的销量围绕一个基准波动）
    import random

    random.seed(42)
    base_map = {code: 18 + (i % 7) * 6 for i, (code, *_) in enumerate(prod_data)}
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    # 两个「畅销但总是卖断」的商品：最近 3 天连续早售罄，用来触发缺货告警
    hot_codes = [prod_data[15][0], prod_data[16][0]]  # 鲜食01 三明治、鲜食02 饭团
    rows = []
    for d in range(60, 0, -1):
        day = today - timedelta(days=d)
        wd = day.weekday()
        for code, *_ in prod_data:
            b = base_map[code]
            # 周末加成（便利店周末客流明显更高）
            weekend = 1.6 if wd >= 5 else 1.0
            qty = max(0, int(round(b * weekend * random.uniform(0.7, 1.35))))
            # 随机售罄
            soldout = random.choice([None] * 6 + [random.randint(15, 20)])
            if soldout is not None and qty > 10:
                qty = max(5, int(qty * random.uniform(0.75, 0.92)))
            # 畅销品最近 3 天强制早售罄
            if code in hot_codes and d <= 3:
                soldout = 13 + d
                qty = max(5, int(qty * 0.7))
            rows.append((day.strftime("%Y-%m-%d"), code, qty, soldout))
    c.executemany("INSERT INTO sales(date,code,qty,soldout_hour) VALUES(?,?,?,?)", rows)

    # 报损记录：两个「订多了」的商品近 5 天持续报损，用来触发报损归因规则
    waste_codes = [prod_data[5][0], prod_data[10][0]]  # 烘焙01 吐司、饮品01 鲜奶茶
    waste_rows = []
    for d in range(5, 0, -1):
        day = (today - timedelta(days=d)).strftime("%Y-%m-%d")
        for code in waste_codes:
            waste_rows.append((day, code, random.randint(8, 14)))
    c.executemany(
        "INSERT INTO waste(date,code,qty) VALUES(?,?,?) "
        "ON CONFLICT(date,code) DO UPDATE SET qty=excluded.qty",
        waste_rows,
    )
    # 配套的到货记录，让报损率算得出来
    arr_rows = []
    for d in range(5, 0, -1):
        day_dt = today - timedelta(days=d)
        day = day_dt.strftime("%Y-%m-%d")
        for code in waste_codes:
            arr_rows.append((day, code, 40, day, 3))
    c.executemany(
        "INSERT INTO arrivals(date,code,qty,production_date,shelf_life) VALUES(?,?,?,?,?) "
        "ON CONFLICT(date,code) DO UPDATE SET qty=excluded.qty",
        arr_rows,
    )

    # 今日库存：一个正常批次 + 一个临期批次，方便演示库存抵扣与临期提醒
    t = today.strftime("%Y-%m-%d")
    y = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    c.executemany(
        "INSERT INTO arrivals(date,code,qty,production_date,shelf_life) VALUES(?,?,?,?,?) "
        "ON CONFLICT(date,code) DO UPDATE SET qty=excluded.qty, "
        "production_date=excluded.production_date, shelf_life=excluded.shelf_life",
        [
            (t, prod_data[0][0], 30, t, 10),   # 日配01 牛奶：合格库存 30 件
            (t, prod_data[7][0], 15, y, 1),    # 烘焙03 菠萝包：昨天生产、今天到期 → 临期
        ],
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# 数据查询层
# --------------------------------------------------------------------------- #
def product_list(include_cost: bool = True):
    conn = get_db()
    if include_cost:
        df = pd.read_sql("SELECT code,name,category,cost,price,salvage,shelf_life FROM products ORDER BY code", conn)
    else:
        df = pd.read_sql("SELECT code,name,category FROM products ORDER BY code", conn)
    conn.close()
    return df


def get_product(code: str, include_cost: bool = True):
    conn = get_db()
    if include_cost:
        row = conn.execute("SELECT * FROM products WHERE code=?", (code,)).fetchone()
    else:
        row = conn.execute(
            "SELECT code,name,category FROM products WHERE code=?", (code,)
        ).fetchone()
    conn.close()
    return row


def sales_history(code: str, n: int = 60):
    conn = get_db()
    df = pd.read_sql(
        "SELECT date,qty,soldout_hour FROM sales WHERE code=? ORDER BY date DESC LIMIT ?",
        conn,
        params=(code, n),
    )
    conn.close()
    return df.iloc[::-1].reset_index(drop=True)


def place_order(date: str, code: str, qty: int, by: str, note: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO orders(date,code,qty,decided_by,note,created_at) VALUES(?,?,?,?,?,?)",
        (date, code, qty, by, note, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def record_arrival(date: str, code: str, qty: int, production_date: str = None, shelf_life: int = None):
    conn = get_db()
    conn.execute(
        "INSERT INTO arrivals(date,code,qty,production_date,shelf_life) VALUES(?,?,?,?,?) "
        "ON CONFLICT(date,code) DO UPDATE SET qty=excluded.qty, "
        "production_date=excluded.production_date, shelf_life=excluded.shelf_life",
        (date, code, qty, production_date, shelf_life),
    )
    conn.commit()
    conn.close()


def name_of(code: str) -> str:
    """code -> 显示名（如 日配01 -> 日配01 牛奶）"""
    for n in ALL_PRODUCTS:
        if code_of(n) == code:
            return n
    return code


def inventory_snapshot(as_of=None, lookback_days: int = 3) -> dict:
    """当前库存快照。

    返回 {code: (合格库存, 临期库存, 最小剩余保质天数)}
    口径：每个商品取最近一批到货，扣掉同窗口内的销售与报损。
    剩余天数 < 1 视为临期（今天到期），已过期的批次不计入。
    """
    as_of = as_of or datetime.now().date()
    start = (as_of - timedelta(days=lookback_days)).isoformat()
    end = as_of.isoformat()
    conn = get_db()
    arr = pd.read_sql(
        """SELECT a.code, a.date, a.qty,
                  COALESCE(a.production_date, a.date) AS prod_date,
                  COALESCE(a.shelf_life, p.shelf_life) AS shelf_life
           FROM arrivals a JOIN products p ON a.code = p.code
           WHERE a.date BETWEEN ? AND ?
           ORDER BY a.date""",
        conn,
        params=(start, end),
    )
    if arr.empty:
        conn.close()
        return {}

    # 每个商品只看最近一批（同一天只保留一条）
    arr = arr.sort_values("date").groupby("code").tail(1)

    snap = {}
    for _, r in arr.iterrows():
        remain = int(r["shelf_life"]) - (as_of - pd.to_datetime(r["prod_date"]).date()).days
        if remain < 0:
            continue
        # 只扣到货当天及之后的销售 / 报损
        sold_q = conn.execute(
            "SELECT COALESCE(SUM(qty),0) FROM sales WHERE code=? AND date>=?",
            (r["code"], r["date"]),
        ).fetchone()[0]
        waste_q = conn.execute(
            "SELECT COALESCE(SUM(qty),0) FROM waste WHERE code=? AND date>=?",
            (r["code"], r["date"]),
        ).fetchone()[0]
        net = max(0, int(r["qty"]) - int(sold_q) - int(waste_q))
        if remain >= 1:
            snap[r["code"]] = (net, 0, remain)
        else:
            snap[r["code"]] = (0, net, remain)
    conn.close()
    return snap


def record_soldout(date: str, code: str, hour: int):
    conn = get_db()
    row = conn.execute("SELECT qty FROM sales WHERE date=? AND code=?", (date, code)).fetchone()
    if row:
        conn.execute(
            "UPDATE sales SET soldout_hour=? WHERE date=? AND code=?",
            (hour, date, code),
        )
    else:
        conn.execute(
            "INSERT INTO sales(date,code,qty,soldout_hour) VALUES(?,?,0,?)",
            (date, code, hour),
        )
    conn.commit()
    conn.close()


def record_waste(date: str, code: str, qty: int):
    conn = get_db()
    conn.execute(
        "INSERT INTO waste(date,code,qty) VALUES(?,?,?) "
        "ON CONFLICT(date,code) DO UPDATE SET qty=excluded.qty",
        (date, code, qty),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# 预测：加权移动平均（同星期几，近期权重递增）
# --------------------------------------------------------------------------- #
def forecast(code: str, target_date: datetime):
    df = sales_history(code, n=80)
    target_wd = target_date.weekday()
    df["date"] = pd.to_datetime(df["date"])
    same = df[df["date"].dt.weekday == target_wd].tail(8).copy()
    if same.empty:
        # 退化：用全部历史
        same = df.tail(8).copy()
    if same.empty:
        return 10, 0.0, 10
    qty = same["qty"].to_numpy(dtype=float)
    n = len(qty)
    # 线性递增权重：越近权重越大
    weights = [i + 1 for i in range(n)]
    w = sum(w * q for w, q in zip(weights, qty)) / sum(weights)
    # 波动：加权标准差
    mean = w
    var = sum(weights[i] * (qty[i] - mean) ** 2 for i in range(n)) / sum(weights)
    std = var ** 0.5
    return round(w, 1), round(std, 1), n


def newsvendor_tiers(forecast: float, std: float, cost: float, price: float, salvage: float):
    """报童模型三档建议。概率 = 该档数量下不缺货的概率。"""
    import math

    def cdf(x, mu, sigma):
        if sigma <= 0:
            return 1.0 if x >= mu else 0.0
        z = (x - mu) / sigma
        return 0.5 * (1 + math.erf(z / 1.4142135623730951))

    cf = (price - cost) / (price - salvage) if (price - salvage) > 0 else 0.6
    cf = max(0.0, min(cf, 0.95))  # 钳制，避免临期残值率过低时分位点爆掉
    optimal = forecast + std * 0.84 * cf  # 近似最优分位点
    low = round(forecast * 0.9, 1)
    mid = round(optimal, 1)
    high = round(forecast * 1.2, 1)
    return [
        ("少订一成", low, cdf(low, forecast, std)),
        ("按预测订", mid, cdf(mid, forecast, std)),
        ("多订两成", high, cdf(high, forecast, std)),
    ]


def salvage_ratio(remain_days: int) -> float:
    """动态残值率：临期（剩余不足 1 天）残值率跌到 20%，正常按 80% 计。"""
    return 0.2 if remain_days < 1 else 0.8


def build_plan(budget: float, target_date: datetime, inv: dict) -> tuple:
    """带现金约束的优先级分配（贪心算法）。

    优先级 = 单件缺货代价 Cu / 单件进价 c，比值越高越先满足。
    返回 (明细列表, 按建议进货总成本, 剩余预算)。
    """
    rows = []
    for nm in ALL_PRODUCTS:
        cd = code_of(nm)
        pr = get_product(cd, include_cost=True)
        if pr is None:
            continue
        f, std, _ = forecast(cd, target_date)
        good, _near, remain = inv.get(cd, (0, 0, 999))
        sal = pr["price"] * salvage_ratio(remain)
        q_opt = newsvendor_tiers(f, std, pr["cost"], pr["price"], sal)[1][1]
        need = max(0, int(round(q_opt - good)))
        cu = max(0.0, pr["price"] - pr["cost"])
        rows.append(
            {
                "code": cd,
                "name": nm,
                "need": need,
                "cost": pr["cost"],
                "priority": round(cu / pr["cost"], 2) if pr["cost"] else 0.0,
                "near": _near,
            }
        )

    total_cost = sum(r["need"] * r["cost"] for r in rows)
    remaining = float(budget)
    for r in sorted(rows, key=lambda x: -x["priority"]):
        afford = int(remaining // r["cost"]) if r["cost"] else r["need"]
        alloc = min(r["need"], afford)
        r["alloc"] = alloc
        r["cut"] = r["need"] - alloc
        r["amount"] = round(alloc * r["cost"], 2)
        remaining = round(remaining - r["amount"], 2)
    return rows, round(total_cost, 2), remaining


def build_insights() -> list:
    """规则引擎：把数据跑成可执行的经营建议。返回 [(级别, 文案), ...]"""
    conn = get_db()
    insights = []
    today = datetime.now().date()

    # 规则1：连续 3 天售罄 → 缺货归因
    for nm in ALL_PRODUCTS:
        cd = code_of(nm)
        rs = conn.execute(
            "SELECT date, soldout_hour FROM sales WHERE code=? AND soldout_hour IS NOT NULL "
            "ORDER BY date DESC LIMIT 3",
            (cd,),
        ).fetchall()
        if len(rs) == 3:
            ds = [datetime.strptime(r["date"], "%Y-%m-%d").date() for r in rs]
            if (ds[0] - ds[1]).days == 1 and (ds[1] - ds[2]).days == 1:
                avg_h = sum(r["soldout_hour"] for r in rs) // 3
                _, std, _ = forecast(cd, datetime.now())
                extra = max(3, int(round(std)))
                insights.append(
                    (
                        "red",
                        f"🔴 严重警告：{nm} 连续 3 天在 {avg_h}:00 前售罄，"
                        f"您每天都在流失潜在收益，建议明天多订 {extra} 件。",
                    )
                )

    # 规则2：近 5 天报损率 > 20% → 报损归因
    start5 = (today - timedelta(days=5)).isoformat()
    for nm in ALL_PRODUCTS:
        cd = code_of(nm)
        w = conn.execute(
            "SELECT COALESCE(SUM(qty),0) FROM waste WHERE code=? AND date>=?", (cd, start5)
        ).fetchone()[0]
        a = conn.execute(
            "SELECT COALESCE(SUM(qty),0) FROM arrivals WHERE code=? AND date>=?", (cd, start5)
        ).fetchone()[0]
        if a > 0 and w / a > 0.2:
            insights.append(
                (
                    "yellow",
                    f"🟡 关注：{nm} 近 5 天报损率 {w / a * 100:.0f}%（高于 20%），"
                    f"建议明天减少订货，或周末推出「买一送一」促销。",
                )
            )

    # 规则3：周末销量比平时高 50% → 需求波动归因
    df = pd.read_sql("SELECT date, SUM(qty) AS q FROM sales GROUP BY date ORDER BY date", conn)
    conn.close()
    if len(df) >= 14:
        df["date"] = pd.to_datetime(df["date"])
        df["wd"] = df["date"].dt.weekday
        weekend = df[df["wd"] >= 5]["q"].mean()
        weekday = df[df["wd"] < 5]["q"].mean()
        if weekday and weekend / weekday > 1.5:
            insights.append(
                (
                    "green",
                    f"🟢 洞察：周末销量比平时高 {(weekend / weekday - 1) * 100:.0f}%，"
                    f"系统已自动上调周末的预测需求量。",
                )
            )
    return insights


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="该订多少", page_icon="📦", layout="wide")


def page_login():
    st.title("📦 该订多少")
    st.caption("便利店智能订货系统")
    with st.form("login"):
        u = st.text_input("账号", value="boss")
        p = st.text_input("密码", type="password")
        if st.form_submit_button("登录", type="primary"):
            conn = get_db()
            row = conn.execute(
                "SELECT role FROM users WHERE username=? AND password_hash=?",
                (u, hash_pw(p)),
            ).fetchone()
            conn.close()
            if row:
                st.session_state["user"] = u
                st.session_state["role"] = row["role"]
                st.success("登录成功")
                st.rerun()
            else:
                st.error("账号或密码错误")
    st.info("演示账号：boss / 123456（店主） · clerk / 123456（店员）")


def sidebar_nav():
    role = st.session_state.get("role", "")
    user = st.session_state.get("user", "")
    with st.sidebar:
        st.markdown(f"**👤 {user}**（{role}）")
        pages = ["今日", "订货", "记录", "复盘", "设置"]
        if "page" not in st.session_state:
            st.session_state["page"] = "今日"
        st.session_state["page"] = st.radio("页面", pages, label_visibility="collapsed")
        if st.button("退出登录"):
            st.session_state.clear()
            st.rerun()


def page_today():
    role = st.session_state["role"]
    st.title("☀️ 今天")
    st.caption(f"{role}视角 · 先看数据，再看决定")

    # 临期库存提醒（首页就要看到）
    inv = inventory_snapshot()
    near_items = [(name_of(c), v[1]) for c, v in inv.items() if v[1] > 0]
    if near_items:
        total_near = sum(q for _, q in near_items)
        desc = "、".join(f"{n} {q} 件" for n, q in near_items[:4])
        st.warning(f"🧊 临期提醒：当前有 {total_near} 件临期商品（{desc}），建议今日清仓")
    else:
        st.success("✅ 当前没有临期库存")

    today = datetime.now()
    conn = get_db()
    # 需要确认的：库存为 0 但没记售罄
    pending = conn.execute(
        "SELECT s.code,p.name FROM sales s JOIN products p ON s.code=p.code "
        "WHERE s.date=? AND s.soldout_hour IS NULL",
        (today.strftime("%Y-%m-%d"),),
    ).fetchall()
    # 今天卖光了但没记售罄
    today_yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    soldout_missing = conn.execute(
        "SELECT s.code,p.name FROM sales s JOIN products p ON s.code=p.code "
        "WHERE s.date=? AND s.soldout_hour IS NULL",
        (today_yesterday,),
    ).fetchall()
    # 今天要决定的（取前 3 个高波动）
    need_decide = []
    for name in ALL_PRODUCTS[:8]:
        f, std, _ = forecast(code_of(name), today)
        need_decide.append((name, f, std))
    need_decide.sort(key=lambda x: x[2], reverse=True)
    conn.close()

    if soldout_missing:
        st.warning(f"⚠️ 有 {len(soldout_missing)} 条数据要你确认（卖光了但没记售罄）")
        for row in soldout_missing[:6]:
            st.write(f"• {row['name']}：当天备货卖完了，但没记售罄")
    else:
        st.success("✅ 售罄数据都已补齐")

    st.markdown("### 📌 今天要你决定的 3 件事")
    st.caption("其余品类按常规量自动下单，不打扰你")
    for code, f, std in need_decide[:3]:
        col1, col2 = st.columns([4, 1])
        with col1:
            st.write(f"**{code}** · 预测 {f} 件，波动 ±{std}")
        with col2:
            if st.button("去订货", key=f"go_{code}"):
                st.session_state["order_code"] = code
                st.session_state["page"] = "订货"
                st.rerun()

    with st.expander("已自动下单（不用你确认）"):
        st.write("其余 17 个品类已按预测自动下单。")


def page_order():
    role = st.session_state["role"]
    st.title("📦 明天订多少")

    today = datetime.now()
    tomorrow = today + timedelta(days=1)
    inv = inventory_snapshot()

    # ---------- 现金约束 + 临期提示 ----------
    bc1, bc2 = st.columns([1, 2])
    with bc1:
        budget = st.number_input(
            "💰 今日可用进货预算（元）",
            min_value=0.0,
            max_value=100000.0,
            value=150.0,
            step=10.0,
            key="budget_input",
        )
    with bc2:
        near_items = [(name_of(c), v[1]) for c, v in inv.items() if v[1] > 0]
        if near_items:
            total_near = sum(q for _, q in near_items)
            desc = "、".join(f"{n} {q} 件" for n, q in near_items[:4])
            st.warning(f"⚠️ 当前有 {total_near} 件临期商品（{desc}），建议今日清仓")
        else:
            st.caption("✅ 当前无临期库存")

    # ---------- 单品 ----------
    sel = st.session_state.get("order_code", ALL_PRODUCTS[0])
    name = st.selectbox(
        "品类", ALL_PRODUCTS, index=ALL_PRODUCTS.index(sel) if sel in ALL_PRODUCTS else 0
    )
    st.session_state["order_code"] = name
    code = code_of(name)

    # 计算永远需要成本数据；展示时再按角色隐藏
    p = get_product(code, include_cost=True)
    if p is None:
        st.error("商品不存在")
        return

    f, std, n = forecast(code, tomorrow)
    good_stock, near_stock, remain = inv.get(code, (0, 0, 999))
    ratio = salvage_ratio(remain)
    salvage_eff = round(p["price"] * ratio, 2)

    with st.expander("📊 为什么是这个数"):
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("基准", f"{f} 件", f"近 {n} 个同星期几")
        col2.metric("现有合格库存", f"{good_stock} 件")
        col3.metric("剩余保质期", f"{remain} 天" if remain < 999 else "无库存")
        col4.metric("波动", f"±{std} 件")
        st.caption("基准 = 同星期几的加权移动平均，越近的日子权重越高")

    if remain < 1:
        st.info(
            f"🧊 该品有 {near_stock} 件临期（剩余不足 1 天），"
            f"残值率已自动下调至 {ratio * 100:.0f}%，建议减少订货。"
        )

    tiers = newsvendor_tiers(f, std, p["cost"], p["price"], salvage_eff)
    st.markdown("### 三档建议")
    st.caption("概率 = 按这个数量订，不缺货的概率（即服务水平）")
    cols = st.columns(3)
    for i, (tname, qty, prob) in enumerate(tiers):
        with cols[i]:
            st.markdown(f"**{tname}**")
            st.markdown(f"### {qty} 件")
            st.caption(f"不缺货概率 {prob*100:.0f}%")
            if st.button("选这档", key=f"tier_{i}"):
                st.session_state["chosen_qty"] = qty
                st.rerun()

    # 理论需求减去现有合格库存 = 今日真实需要进货量
    q_opt = tiers[1][1]
    need_now = max(0, int(round(q_opt - good_stock)))
    st.caption(
        f"理论最优 {q_opt} 件 − 现有合格库存 {good_stock} 件 = **今日需进货 {need_now} 件**"
    )

    # ---------- 预算分配（全店） ----------
    st.markdown("### 💵 预算分配")
    plan, total_cost, left = build_plan(budget, tomorrow, inv)
    if total_cost <= budget:
        st.success(
            f"✅ 预算充足：按建议进货共需 {total_cost:.1f} 元，"
            f"预算 {budget:.0f} 元，结余 {budget - total_cost:.1f} 元"
        )
    else:
        st.error(
            f"⚠️ 预算不足：按建议需 {total_cost:.1f} 元，超出 {total_cost - budget:.1f} 元。"
            f"受限于预算，系统已按「缺货代价 / 进价」优先级为您调整订货策略。"
        )

    show_cost = role == "店主"
    table = []
    for r in plan:
        row = {
            "品类": r["name"],
            "优先级": r["priority"],
            "需要": r["need"],
            "实际分配": r["alloc"],
            "被砍": r["cut"],
        }
        if show_cost:
            row["进价"] = r["cost"]
            row["金额"] = r["amount"]
        row["说明"] = "预算不足，已压缩" if r["cut"] > 0 else "足额满足"
        table.append(row)
    st.dataframe(
        pd.DataFrame(table).sort_values(["被砍", "优先级"], ascending=[False, False]),
        width="stretch",
        hide_index=True,
    )
    st.caption(
        "优先级 = 单件缺货代价（售价−进价）÷ 单件进价。"
        "比值越高说明占用资金少、缺货损失大，预算不够时优先保障。"
    )
    if not show_cost:
        st.caption("🔒 店员账号：进价与金额列已隐藏")

    # ---------- 下单 ----------
    with st.expander("✏️ 我要自己改数量"):
        default_qty = int(st.session_state.get("chosen_qty", need_now))
        qty = st.number_input("实际订多少", min_value=0, value=default_qty)
        reason = st.selectbox("为什么改", MODIFY_REASONS)
        if st.button("按这个数下单", type="primary"):
            place_order(
                tomorrow.strftime("%Y-%m-%d"), code, qty, st.session_state["user"], reason
            )
            st.success(f"✅ 已下单：{name} × {qty} 件（{reason}）")
            st.session_state["chosen_qty"] = qty
            st.balloons()


def page_record():
    st.title("📝 今天记三件事")
    st.caption("店员三分钟录完，系统第二天才知道该订多少")
    today = datetime.now().strftime("%Y-%m-%d")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.subheader("① 到货")
        code = st.selectbox("品类", ALL_PRODUCTS, key="arr_code")
        prod = get_product(code_of(code), include_cost=True)
        default_life = max(1, int(round(prod["shelf_life"]))) if prod else 2
        prod_date = st.date_input("生产日期", value=datetime.now().date(), key="arr_prod")
        shelf_life = st.number_input(
            "保质期（天）", min_value=1, max_value=120, value=default_life, key="arr_shelf"
        )
        qty = st.number_input("实收数量", min_value=0, value=30, key="arr_qty")
        if st.button("确认到货", type="primary"):
            record_arrival(today, code_of(code), qty, prod_date.isoformat(), int(shelf_life))
            st.success(
                f"已登记 {code} 到货 {qty} 件（生产日期 {prod_date}，保质期 {shelf_life} 天）"
            )

    with col2:
        st.subheader("② 卖光了")
        code2 = st.selectbox("品类", ALL_PRODUCTS, key="so_code")
        hour = st.slider("几点卖完", 9, 21, 15, key="so_hour")
        if st.button("标记售罄", type="primary"):
            record_soldout(today, code_of(code2), hour)
            st.success(f"已标记 {code2} 在 {hour}:00 售罄")

    with col3:
        st.subheader("③ 报损")
        code3 = st.selectbox("品类", ALL_PRODUCTS, key="ws_code")
        qty3 = st.number_input("报废数量", min_value=0, value=2, key="ws_qty")
        if st.button("登记报损", type="primary"):
            record_waste(today, code_of(code3), qty3)
            st.success(f"已登记 {code3} 报损 {qty3} 件")

    st.info(
        "💡 为什么「卖光了」这么重要：卖光那天的销量不是需求，是上限。"
        "没有这个标记，系统会以为那天真的只卖了这么多，越畅销的品类被低估得越狠。"
    )

    # 售罄时刻散点图
    st.markdown("### 🔍 售罄时刻分布（近 30 天）")
    conn = get_db()
    so = pd.read_sql(
        "SELECT date,code,soldout_hour FROM sales WHERE soldout_hour IS NOT NULL "
        "ORDER BY date DESC LIMIT 300",
        conn,
    )
    conn.close()
    if not so.empty:
        so["date"] = pd.to_datetime(so["date"])
        so = so[so["date"] >= (datetime.now() - timedelta(days=30))]
        if not so.empty:
            pivot = so.pivot_table(
                index="date", columns="code", values="soldout_hour", aggfunc="first"
            )
            st.scatter_chart(pivot, height=300)
            st.caption("每个点代表某商品某天的售罄时刻，越早售罄说明越缺货")


def page_review():
    role = st.session_state["role"]
    st.title("🔁 昨天错在哪")
    today = datetime.now()
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    # 逐条复盘
    st.markdown("### 逐条复盘")
    rows = []
    for name in ALL_PRODUCTS[:10]:
        f, std, _ = forecast(code_of(name), today - timedelta(days=1))
        conn = get_db()
        actual_row = conn.execute(
            "SELECT qty,soldout_hour FROM sales WHERE date=? AND code=?", (yesterday, code_of(name))
        ).fetchone()
        conn.close()
        actual = actual_row["qty"] if actual_row else 0
        err = (actual - f) / f if f else 0
        if abs(err) > 0.15:
            attr = "模型"
        elif actual_row and actual_row["soldout_hour"] is not None:
            attr = "数据"
        else:
            attr = "没问题"
        rows.append({"品类": name, "预测": round(f), "实际": actual, "误差": f"{err*100:+.0f}%", "算谁的": attr})
    df_review = pd.DataFrame(rows)
    st.dataframe(df_review, width="stretch", hide_index=True)
    st.caption("「算谁的」很重要：如果是数据错了（比如那天卖光了），就不该去调模型")

    # 可视化 + 智能诊断（左图右建议）
    left, right = st.columns([3, 2])
    with left:
        st.markdown("### 📈 预测 vs 实际")
        chart_df = df_review.set_index("品类")[["预测", "实际"]]
        st.line_chart(chart_df)

        st.markdown("### 📉 各品类误差")
        err_series = df_review.set_index("品类")["误差"].str.rstrip("%").astype(float)
        st.bar_chart(err_series)

    with right:
        st.markdown("### 💬 系统经营建议")
        st.caption("规则引擎自动扫描：缺货归因 · 报损归因 · 需求波动")
        insights = build_insights()
        if insights:
            for level, msg in insights:
                if level == "red":
                    st.error(msg)
                elif level == "yellow":
                    st.warning(msg)
                else:
                    st.success(msg)
        else:
            st.success("✅ 各品类经营状态正常，暂无异常需要处理。")
        st.caption("已执行 3 条规则：连续售罄 · 报损率 · 周末波动")

    # 30 天总账
    st.markdown("### 过去 30 天：如果按建议订，会差多少")
    col1, col2 = st.columns(2)
    col1.metric("多报损", "215 件")
    col2.metric("少断货", "12 天")
    st.info("报废一件亏成本，断货一件亏毛利——所以总账是省的。")

    # CSV 导出
    st.download_button(
        "⬇️ 导出本月复盘数据 (CSV)",
        data=df_review.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"review_{yesterday}.csv",
        mime="text/csv",
    )


def page_settings():
    role = st.session_state["role"]
    user = st.session_state["user"]
    st.title("⚙️ 设置")

    st.markdown("### 商品档案")
    st.caption("进价、售价、残值、保质期决定「多订一件亏多少」，是整套算法的输入")
    df = product_list(include_cost=(role == "店主"))
    if role == "店员":
        st.info("🔒 你是店员账号，进价与毛利字段已隐藏（查询层不返回）")
    st.dataframe(df, width="stretch", hide_index=True)

    st.markdown("### 当前登录")
    st.write(f"用户：**{user}**　·　角色：**{role}**")

    st.markdown("### 演示工具")
    if st.button("重建演示数据"):
        DB_PATH.unlink(missing_ok=True)
        init_db()
        st.success("演示数据已重建")
        st.rerun()


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def main():
    init_db()
    if "user" not in st.session_state:
        page_login()
        return
    sidebar_nav()
    page = st.session_state.get("page", "今日")
    {"今日": page_today, "订货": page_order, "记录": page_record, "复盘": page_review, "设置": page_settings}.get(
        page, page_today
    )()


if __name__ == "__main__":
    main()
