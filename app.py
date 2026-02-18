from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request  
import json
import os
from openai import OpenAI

import sqlite3


app = Flask(__name__)

DB_PATH = "candles.db"
PUSH_TOKEN = os.environ.get("PUSH_TOKEN")
if not PUSH_TOKEN:
    raise RuntimeError("PUSH_TOKEN not set")

DB_PATH = os.environ.get("CANDLES_DB_PATH", "candles.db")

PUSH_TOKEN = os.environ.get("PUSH_TOKEN")
if not PUSH_TOKEN:
    raise RuntimeError("PUSH_TOKEN not set")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            code TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            t TEXT NOT NULL,
            o REAL NOT NULL,
            h REAL NOT NULL,
            l REAL NOT NULL,
            c REAL NOT NULL,
            v REAL NOT NULL,
            PRIMARY KEY (code, timeframe, t)
        )
    """)
    conn.commit()
    conn.close()

init_db()


# ---------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

def _to_float(s: str) -> float:
    return float(str(s).replace(",", "").strip())

# ---------------------------------------------------------------------
# Index (KOSPI/KOSDAQ current)
# ---------------------------------------------------------------------
NAVER_INDEX_URLS = {
    "KOSPI": "https://finance.naver.com/sise/sise_index.naver?code=KOSPI",
    "KOSDAQ": "https://finance.naver.com/sise/sise_index.naver?code=KOSDAQ",
}

def fetch_naver_index(code: str) -> dict:
    """
    Returns: dict(price, change, changeRate)
    """
    url = NAVER_INDEX_URLS[code]
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    html = r.text

    # 현재지수
    m_now = re.search(r'id="now_value"[^>]*>\s*([0-9\.,]+)\s*<', html)
    if not m_now:
        m_now = re.search(
            r'현재지수</span>\s*<em[^>]*>\s*<span[^>]*>([0-9\.,]+)</span>',
            html,
            re.S,
        )
    if not m_now:
        raise RuntimeError(f"Failed to parse {code} now value")

    price = _to_float(m_now.group(1))

    # 전일대비
    m_chg = re.search(r'id="change_value"[^>]*>\s*([0-9\.,]+)\s*<', html)
    if not m_chg:
        m_chg = re.search(r'전일대비</span>.*?<span[^>]*class="tah">([0-9\.,]+)</span>', html, re.S)
    change = _to_float(m_chg.group(1)) if m_chg else None

    # 등락률
    m_rate = re.search(r'id="change_rate"[^>]*>\s*([0-9\.,]+)\s*<', html)
    if not m_rate:
        m_rate = re.search(r'등락률</span>.*?<span[^>]*class="tah">([0-9\.,]+)</span>', html, re.S)
    change_rate = _to_float(m_rate.group(1)) if m_rate else None

    # 부호 처리(간단): no_down 표시가 있으면 음수로
    # (정확도를 높이려면 change 영역 주변의 class만 판별하도록 개선 가능)
    if "no_down" in html:
        if change is not None:
            change = -abs(change)
        if change_rate is not None:
            change_rate = -abs(change_rate)

    return {"price": price, "change": change, "changeRate": change_rate}

# ---------------------------------------------------------------------
# Index series (daily points via siseJson)
# ---------------------------------------------------------------------
NAVER_SISEJSON_URL = "https://api.finance.naver.com/siseJson.naver"

def _yyyymmdd(d: datetime) -> str:
    return d.strftime("%Y%m%d")

def fetch_naver_daily_points(symbol: str, days: int = 60) -> list[dict]:
    """
    Returns: [{"t":"YYYY-MM-DD","v":float}, ...]
    """
    end = datetime.now()
    start = end - timedelta(days=days * 2)

    params = {
        "symbol": symbol,
        "requestType": "1",
        "startTime": _yyyymmdd(start),
        "endTime": _yyyymmdd(end),
        "timeframe": "day",
    }
    r = requests.get(NAVER_SISEJSON_URL, params=params, headers=HEADERS, timeout=10)
    r.raise_for_status()

    data = r.text.strip()
    arr = ast.literal_eval(data)  # 네이버가 JS array 형태로 내려줘서 이렇게 파싱

    header = arr[0]
    rows = arr[1:]

    i_date = header.index("날짜")
    i_close = header.index("종가")

    pts = []
    for row in rows:
        d = row[i_date]   # "20220103"
        c = row[i_close]  # "1234" or number-string
        try:
            t = datetime.strptime(d, "%Y%m%d").strftime("%Y-%m-%d")
            v = _to_float(c)
            pts.append({"t": t, "v": v})
        except Exception:
            continue

    return pts[-days:]

# ---------------------------------------------------------------------
# News (Naver News economy section)
# ---------------------------------------------------------------------
NAVER_ECON_NEWS_URL = "https://news.naver.com/section/101"

def fetch_naver_econ_news(limit: int = 10) -> list[dict]:
    """
    Returns: [{"title","link","press","ts"}...]
    """
    r = requests.get(NAVER_ECON_NEWS_URL, headers=HEADERS, timeout=10)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    items = []
    # 섹션 페이지에서 기사 타이틀 앵커
    for a in soup.select("a.sa_text_title")[: max(limit * 2, limit)]:
        title = a.get_text(strip=True)
        link = a.get("href", "").strip()
        if not title or not link:
            continue

        # 같은 카드 내 언론사/시간을 찾기 위해 부모 컨테이너 기준으로 탐색
        container = a.find_parent()
        press = None
        ts = None

        if container:
            press_el = container.select_one(".sa_text_press")
            if press_el:
                press = press_el.get_text(strip=True)

            time_el = container.select_one(".sa_text_datetime")
            if time_el:
                ts = time_el.get_text(strip=True)

        items.append({
            "title": title,
            "link": link,
            "press": press,
            "ts": ts,
        })

        if len(items) >= limit:
            break

    return items

# ---------------------------------------------------------------------
# Calendar (simple JSON storage)
# ---------------------------------------------------------------------
CALENDAR_STORE = os.path.join(os.path.dirname(__file__), "calendar_events.json")

def _load_calendar() -> dict:
    try:
        with open(CALENDAR_STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

def _save_calendar(data: dict) -> None:
    tmp = CALENDAR_STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CALENDAR_STORE)

# ---------------------------------------------------------------------
# News Report
# ---------------------------------------------------------------------
NEWS_SUMMARY_STORE = os.path.join(os.path.dirname(__file__), "daily_news_summary.json")

def _load_news_summary() -> dict | None:
    try:
        with open(NEWS_SUMMARY_STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception:
        return None

def _save_news_summary(data: dict) -> None:
    tmp = NEWS_SUMMARY_STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, NEWS_SUMMARY_STORE)

def _simple_kor_summary(items: list[dict]) -> str:
    """
    API 키 없을 때도 동작하도록 '간단 요약' fallback.
    (제목/언론사 기반으로 오늘 이슈를 빠르게 훑는 용도)
    """
    lines = []
    for i, n in enumerate(items[:10], start=1):
        press = (n.get("press") or "").strip()
        title = (n.get("title") or "").strip()
        ts = (n.get("ts") or "").strip()
        s = f"{i}. {title}"
        if press or ts:
            meta = " · ".join([x for x in [press, ts] if x])
            s += f" ({meta})"
        lines.append(s)

    if not lines:
        return "표시할 뉴스가 없습니다."

    return (
        "🧠 오늘의 이슈(제목 기반 빠른 요약)\n"
        + "\n".join(lines)
        + "\n\n"
        "✅ 체크포인트\n"
        "- 금리/환율/물가 관련 제목이 많은지\n"
        "- 반도체/AI/2차전지 등 특정 섹터 쏠림이 있는지\n"
        "- 정책/지정학 리스크(관세/전쟁/규제) 키워드가 있는지\n"
    )

def _llm_summary_if_possible(items: list[dict]) -> str | None:
    """
    OPENAI_API_KEY가 설정되어 있고 openai 패키지가 있으면 LLM 요약 사용.
    실패하면 None 반환 → fallback 요약 사용.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("NO API")
        return None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
    except Exception:
        return None
    
    # 뉴스 묶음(제목/언론사/시간/링크)
    bundle = []
    for n in items[:25]:
        bundle.append(
            f"- 제목: {n.get('title','')}\n"
            f"  언론사: {n.get('press','')}\n"
            f"  시간: {n.get('ts','')}\n"
            f"  링크: {n.get('link','')}\n"
        )
    news_bundle = "\n".join(bundle)

    prompt = f"""
너는 거시경제 흐름을 분석하는 시장 애널리스트다.
입력된 한국 경제 뉴스(제목/언론사/시간/링크)를 기반으로, 한국 시장에 국한하지 말고 글로벌 매크로(미국 금리/달러/유가/중국/유럽)와 연결해 '경제의 큰 흐름'을 해석하라.
뉴스를 개별 사건으로 나열하지 말고, (유동성 → 성장/물가 → 정책 → 자산가격) 연결 구조로 설명하라.

[우선순위]
- 아침 07:30~08:30에 보게 될 핵심 이슈(전일 미국/글로벌 영향 포함)와
- 전일 장 마감 직후 15:30~16:30에 나온 한국 이슈
를 최우선으로 묶어 해석하라.
- 장중 속보는 "단기 변동성"으로만 분류하고 메인 결론에는 비중을 낮춰라.

[증거 규칙]
- 최소 10개 기사 이상을 사용하라. (사용 기사 수를 마지막에 표기)
- 각 섹션의 핵심 bullet에는 근거로 기사 번호를 붙여라. 예: (근거: #3, #7)
- 입력에 실제 가격/지표 수치가 없으면 수치를 만들어내지 말고, "연결 가능성"만 제시하라.

[표현 규칙]
- 확정 표현 금지. "~가능성", "~우려", "~시사"로 표현.
- 불필요한 서론 금지. 반복/동의어 중복 금지.
- 단, 너무 축약하지 말 것: 각 섹션별로 최소 bullet 수를 반드시 채워라.

[분량 규칙: 축약 방지]
1) 4~6줄로 시작 요약(문장형)  ← 한 줄만 쓰지 말고, 흐름이 보이게 쓸 것
2) 섹션 2~5는 각 섹션 당 최소 4개 bullet 이상 작성
3) "왜"와 "그래서"가 최소 1번씩 들어가야 한다(원인→파급).

[출력 형식]

0) ✅ 사용 기사 수: N개
   - 가장 영향 큰 기사 TOP3 제목만 짧게 나열(각각 #번호 포함)

1) 🧭 오늘의 경제 흐름 요약(4~6줄)
   - (경기 확장/둔화/혼조/정책 주도/소비 위축 중 1~2개 키워드로 규정)
   - 한국 ↔ 글로벌 연결 고리 1개 이상 포함

2) 💰 자금의 방향(최소 4 bullet)
   - 자금이 선호할 가능성이 있는 곳(위험자산/안전자산/현금/원자재 등)
   - 수급 주체 추정(외국인/기관/개인 중 가능성 언급)
   - (근거: #번호)

3) 🏭 구조적 변화 신호(최소 4 bullet)
   - 산업 경쟁 구도 변화(예: 반도체/자동차/2차전지/플랫폼/건설 등)
   - 정책 방향(규제/지원/재정/무역)
   - 글로벌 리스크(관세/지정학/공급망)
   - 각 bullet마다 단기 이슈인지 구조 신호인지 [단기]/[구조] 태그 달기
   - (근거: #번호)

4) 📉 단기 리스크 요인(최소 4 bullet)
   - 변동성 확대 요인(정책 이벤트/지표 발표/환율/원자재/실적)
   - “트리거(조건)” 형태로 쓰기: "~가 발생하면 ~가능성"
   - (근거: #번호)

5) 🔍 앞으로 주목할 경제 변수(최소 4 bullet)
   - 금리/환율/물가/고용/무역/실적 중 최소 4개를 포함
   - 각 변수마다 “왜 중요한지(한 줄)” + “체크하면 좋은 방향성(한 줄)”을 붙여라

6) 🧪 자기 점검(2~3줄)
   - 이번 요약이 뉴스 나열이 아니라 ‘흐름(원인→파급→조건)’을 제시했는지 평가
   - 방향성(위험선호/회피/혼조)이 명확한지 평가

[입력 뉴스 목록]
{news_bundle}
""".strip()

    resp = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": "사실 기반으로 간결하게 작성해라."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
        max_output_tokens=1200,
    )
    return resp.output_text


@app.get("/api/news/summary")
def api_news_summary():
    """
    새 요약 생성 + 파일 저장 + 반환
    """
    try:
        items = fetch_naver_econ_news(limit=25)
        summary = _llm_summary_if_possible(items)
        if not summary:
            summary = _simple_kor_summary(items)

        payload = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "summary": summary,
            "count": len(items),
        }
        _save_news_summary(payload)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/news/summary/latest")
def api_news_summary_latest():
    data = _load_news_summary()
    if not data:
        return jsonify({"error": "no summary yet"}), 404
    return jsonify(data)

# ---------------------------------------------------------------------
# Stocks: search + candles (simple)
# NOTE:
# - 일/주/월은 네이버 siseJson로 가능
# - 분봉/틱봉은 "진짜 주식앱처럼" 하려면 WMCA OpenAPI TR(체결/분봉/틱봉)로 붙여야 함
# ---------------------------------------------------------------------

def fetch_naver_stock_candles(code: str, tf: str = "day", count: int = 300) -> list[dict]:
    """
    Returns candles:
      [{"time":"YYYY-MM-DD","open":..,"high":..,"low":..,"close":..,"volume":..}, ...]
    """
    end = datetime.now()
    start = end - timedelta(days=max(1200, count * 3))

    params = {
        "symbol": code,
        "requestType": "1",
        "startTime": _yyyymmdd(start),
        "endTime": _yyyymmdd(end),
        "timeframe": tf,  # "day" | "week" | "month"
    }
    r = requests.get(NAVER_SISEJSON_URL, params=params, headers=HEADERS, timeout=10)
    r.raise_for_status()

    arr = ast.literal_eval(r.text.strip())
    header = arr[0]
    rows = arr[1:]

    def idx(name: str) -> int:
        return header.index(name)

    i_date = idx("날짜")
    i_open = idx("시가")
    i_high = idx("고가")
    i_low  = idx("저가")
    i_close= idx("종가")
    i_vol  = header.index("거래량") if "거래량" in header else None

    out = []
    for row in rows:
        try:
            d = row[i_date]  # "20220103"
            t = datetime.strptime(d, "%Y%m%d").strftime("%Y-%m-%d")
            out.append({
                "time": t,
                "open": _to_float(row[i_open]),
                "high": _to_float(row[i_high]),
                "low":  _to_float(row[i_low]),
                "close": _to_float(row[i_close]),
                "volume": _to_float(row[i_vol]) if i_vol is not None else None,
            })
        except Exception:
            continue

    return out[-count:]


@app.get("/api/stocks/search")
def api_stocks_search():
    """
    아주 단순 버전:
    - 6자리 숫자면 그 코드 그대로 반환(이름은 code로 표시)
    - 종목명 검색은: (1) WMCA 코드리스트 TR로 DB 구축 or (2) 별도 코드리스트 파일 준비 필요
    """
    q = (request.args.get("q") or "").strip()
    items = []

    m = re.search(r"(\d{6})", q)
    if m:
        code = m.group(1)
        items.append({"code": code, "name": code})
        return jsonify({"items": items})

    # TODO: 여기부터는 "종목명->코드" 매핑 테이블이 있어야 함
    # 예: stocks_master.json을 만들어두고 검색
    master_path = os.path.join(os.path.dirname(__file__), "stocks_master.json")
    try:
        if os.path.exists(master_path) and q:
            with open(master_path, "r", encoding="utf-8") as f:
                master = json.load(f)  # [{"code":"005930","name":"삼성전자"}, ...]
            q_low = q.lower()
            for it in master:
                if q_low in str(it.get("name","")).lower():
                    items.append({"code": it["code"], "name": it["name"]})
                if len(items) >= 20:
                    break
    except Exception:
        pass

    return jsonify({"items": items})

@app.post("/api/internal/push/candles")
def push_candles():
    token = request.headers.get("X-PUSH-TOKEN", "")
    if token != PUSH_TOKEN:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    candles = data.get("candles") or []

    if not re.fullmatch(r"\d{6}", code):
        return jsonify({"error": "code must be 6 digits"}), 400

    if not isinstance(candles, list) or not candles:
        return jsonify({"error": "candles must be a non-empty list"}), 400

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for cndl in candles:
        try:
            t = cndl["t"]
            o = float(cndl["o"]); h = float(cndl["h"]); l = float(cndl["l"]); c = float(cndl["c"]); v = float(cndl["v"])
        except Exception:
            continue

        cur.execute("""
            INSERT OR REPLACE INTO candles (code, timeframe, t, o, h, l, c, v)
            VALUES (?, '1m', ?, ?, ?, ?, ?, ?)
        """, (code, t, o, h, l, c, v))

    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.get("/api/stocks/candles")
def api_stocks_candles():
    code = (request.args.get("code") or "").strip()
    tf = (request.args.get("tf") or "1d").strip()
    count = int(request.args.get("count") or "300")

    if not re.fullmatch(r"\d{6}", code):
        return jsonify({"error": "code must be 6 digits"}), 400

    # ✅ 1m은 DB에서
    if tf == "1m":
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT t, o, h, l, c, v
            FROM candles
            WHERE code=? AND timeframe='1m'
            ORDER BY t ASC
        """, (code,))
        rows = cur.fetchall()
        conn.close()

        candles = [{
            "time": r[0],
            "open": r[1],
            "high": r[2],
            "low": r[3],
            "close": r[4],
            "volume": r[5],
        } for r in rows][-count:]

        return jsonify({"code": code, "name": code, "tf": tf, "candles": candles})

    # ✅ 1d/1w/1M은 네이버 그대로
    if tf == "1d":
        n_tf = "day"
    elif tf == "1w":
        n_tf = "week"
    elif tf == "1M":
        n_tf = "month"
    else:
        return jsonify({"error": f"unknown tf: {tf}"}), 400

    try:
        candles = fetch_naver_stock_candles(code, tf=n_tf, count=min(max(count, 30), 1200))
        return jsonify({"code": code, "name": code, "tf": tf, "candles": candles})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.route("/")
def home():
    return render_template("index.html")

@app.get("/api/index/current")
def api_index_current():
    try:
        return jsonify({
            "KOSPI": fetch_naver_index("KOSPI"),
            "KOSDAQ": fetch_naver_index("KOSDAQ"),
        })
    except Exception as e:
        return jsonify({
            "KOSPI": {"price": None, "change": None, "changeRate": None, "error": str(e)},
            "KOSDAQ": {"price": None, "change": None, "changeRate": None, "error": str(e)},
        }), 500

@app.get("/api/index/minute")
def api_index_minute():
    kospi = fetch_naver_daily_points("KOSPI", days=60)
    kosdaq = fetch_naver_daily_points("KOSDAQ", days=60)
    return jsonify({
        "KOSPI": {"points": kospi},
        "KOSDAQ": {"points": kosdaq},
    })

@app.get("/api/news")
def api_news():
    try:
        return jsonify({
            "items": fetch_naver_econ_news(limit=10),
            "source": "naver_news_section_101",
            "fetchedAt": datetime.now().isoformat(timespec="seconds"),
        })
    except Exception as e:
        return jsonify({
            "items": [],
            "error": str(e),
        }), 500
        
@app.get("/api/calendar/events")
def api_calendar_get():
    """
    query:
      date=YYYY-MM-DD  (optional)
      month=YYYY-MM    (optional)
    return:
      { "items": { "YYYY-MM-DD": [ {id,title,time,note}, ... ], ... } }
    """
    data = _load_calendar()
    date = (request.args.get("date") or "").strip()
    month = (request.args.get("month") or "").strip()

    if date:
        return jsonify({"items": {date: data.get(date, [])}})
    if month:
        filtered = {k: v for k, v in data.items() if k.startswith(month)}
        return jsonify({"items": filtered})

    return jsonify({"items": data})


@app.post("/api/calendar/events")
def api_calendar_add():
    """
    body json:
      { "date":"YYYY-MM-DD", "title":"...", "time":"HH:MM"(optional), "note":"..."(optional) }
    """
    payload = request.get_json(silent=True) or {}
    date = str(payload.get("date", "")).strip()
    title = str(payload.get("title", "")).strip()
    time = str(payload.get("time", "")).strip()
    note = str(payload.get("note", "")).strip()

    if not date or not title:
        return jsonify({"error": "date and title are required"}), 400

    data = _load_calendar()
    arr = data.get(date, [])
    if not isinstance(arr, list):
        arr = []

    item = {
        "id": str(int(datetime.now().timestamp() * 1000)),
        "title": title,
        "time": time,
        "note": note,
    }
    arr.append(item)

    # 시간 기준 정렬 (빈 값은 뒤로)
    def keyfn(x):
        t = (x.get("time") or "").strip()
        return t if t else "99:99"
    arr.sort(key=keyfn)

    data[date] = arr
    _save_calendar(data)
    return jsonify({"ok": True, "item": item})


@app.delete("/api/calendar/events/<date>/<event_id>")
def api_calendar_delete(date: str, event_id: str):
    data = _load_calendar()
    arr = data.get(date, [])
    if not isinstance(arr, list):
        return jsonify({"ok": True})

    new_arr = [x for x in arr if str(x.get("id")) != str(event_id)]
    if new_arr:
        data[date] = new_arr
    else:
        data.pop(date, None)

    _save_calendar(data)
    return jsonify({"ok": True})

@app.route("/api/internal/push/candles", methods=["POST"])
def push_candles():
    token = request.headers.get("X-PUSH-TOKEN")
    if token != PUSH_TOKEN:
        return {"error": "Unauthorized"}, 403

    data = request.json
    code = data.get("code")
    candles = data.get("candles", [])

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    for candle in candles:
        c.execute("""
            INSERT OR REPLACE INTO candles
            (code, timeframe, t, o, h, l, c, v)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            code,
            "1m",
            candle["t"],
            candle["o"],
            candle["h"],
            candle["l"],
            candle["c"],
            candle["v"],
        ))

    conn.commit()
    conn.close()

    return {"status": "ok"}


# ---------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

