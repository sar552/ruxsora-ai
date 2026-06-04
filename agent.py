#!/usr/bin/env python3
"""
ERPNext Telegram yordamchi-bot  (Claude API + tool use)

Foydalanuvchi Telegram'da oddiy savol yozadi, masalan:
  - "Kecha kim sales invoice kiritgan?"
  - "Hozir invoys qilinmagan delivery note'lar qancha?"
  - "Bu oy eng katta xaridlar kim qildi?"
Bot savolni Claude'ga uzatadi. Claude kerak bo'lsa ERPNext'dan
ma'lumot olib keladigan tool'ni o'zi chaqiradi, natijani tahlil qilib
o'zbekcha javob qaytaradi.

XAVFSIZLIK:
  * Bot ERPNext'ga FAQAT O'QISH (GET) so'rovlarini yuboradi. Hech narsa
    yaratmaydi/o'chirmaydi/o'zgartirmaydi.
  * Faqat ALLOWED_USERS ro'yxatidagi odamlar bot bilan gaplasha oladi.

Kerak:  pip install requests
"""

import io
import os
import ast
import csv
import json
import time
import requests
from pathlib import Path

# ===================== SOZLAMALAR (.env dan yuklanadi) =====================
# Barcha maxfiy qiymatlar agent.py yonidagi .env faylida turadi:
#   ERP_URL, ERP_KEY, ERP_SECRET, ANTHROPIC_KEY, MODEL, TG_TOKEN,
#   COMPANY (ixtiyoriy), ALLOWED_USERS (ro'yxat).
# .env qiymatlari Python sintaksisida ("matn" yoki [123, 456]) yozilgan va
# ast.literal_eval bilan XAVFSIZ o'qiladi (eval ISHLATILMAYDI).
#   * MODEL: eng arzoni "claude-haiku-4-5-20251001". Murakkabroq tahlil uchun
#     "claude-sonnet-4-6" qo'yish mumkin.
#   * ALLOWED_USERS: faqat shu Telegram ID'lar bot bilan gaplasha oladi
#     (o'z ID'ingizni bilish uchun @userinfobot ga yozing).
#   * COMPANY bo'sh bo'lsa, ERPNext'da bitta kompaniya bo'lsa avtomatik aniqlanadi.
# ===========================================================================


def _coerce(val):
    """'.env' qiymatini Python obyektiga aylantiradi (matn/son/ro'yxat).
    literal_eval bo'lmasa, oddiy tirnoqsiz matn sifatida qaytaradi."""
    try:
        return ast.literal_eval(val)
    except (ValueError, SyntaxError):
        return val.strip().strip("\"'")


def _load_env(path=None):
    """agent.py yonidagi .env ni o'qib, sozlamalarni shu modul globallariga yuklaydi.
    Operatsion tizim environment o'zgaruvchisi bo'lsa, u .env dan ustun turadi
    (serverda systemd orqali berishni osonlashtiradi)."""
    env_path = Path(path) if path else Path(__file__).with_name(".env")
    if not env_path.exists():
        raise SystemExit(
            f"DIQQAT: .env fayli topilmadi: {env_path}\n"
            "agent.py yonida .env bo'lishi kerak (ERP_URL, ERP_KEY, ... bilan)."
        )
    g = globals()
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        g[key] = _coerce(os.environ[key]) if key in os.environ else _coerce(val.strip())


# Standart (bo'sh) qiymatlar — IDE/statik tahlil ko'rishi uchun. Haqiqiy
# qiymatlarni _load_env() quyida .env dan o'qib, shularning ustiga yozadi.
ERP_URL = ERP_KEY = ERP_SECRET = ANTHROPIC_KEY = MODEL = TG_TOKEN = COMPANY = ""
ALLOWED_USERS = []

_load_env()

# Majburiy sozlamalar to'g'ri yuklanganini tekshiramiz — bo'lmasa darhol to'xtaymiz.
_required = ["ERP_URL", "ERP_KEY", "ERP_SECRET", "ANTHROPIC_KEY",
             "MODEL", "TG_TOKEN", "ALLOWED_USERS"]
_missing = [k for k in _required if not globals().get(k)]
if _missing:
    raise SystemExit("DIQQAT: .env da quyidagilar yo'q yoki bo'sh: " + ", ".join(_missing))
if not isinstance(ALLOWED_USERS, (list, tuple, set)):
    raise SystemExit("DIQQAT: .env dagi ALLOWED_USERS ro'yxat bo'lishi kerak, masalan: [123, 456]")
COMPANY = globals().get("COMPANY") or ""   # bo'sh => avtomatik aniqlanadi

ERP_HEADERS = {"Authorization": f"token {ERP_KEY}:{ERP_SECRET}"}


def _autodetect_company():
    """COMPANY bo'sh bo'lsa, ERPNext'da bitta kompaniya bo'lsa uni avtomatik oladi."""
    global COMPANY
    if COMPANY:
        return
    try:
        r = requests.get(f"{ERP_URL}/api/resource/Company",
                         headers=ERP_HEADERS,
                         params={"fields": json.dumps(["name"]), "limit_page_length": 5},
                         timeout=30)
        r.raise_for_status()
        comps = r.json().get("data", [])
        if len(comps) == 1:
            COMPANY = comps[0]["name"]
            print(f"Kompaniya avtomatik aniqlandi: {COMPANY}")
        elif len(comps) > 1:
            print(f"DIQQAT: {len(comps)} ta kompaniya bor. SOZLAMALAR'da COMPANY ni to'ldiring.")
    except Exception as e:
        print("Kompaniyani aniqlab bo'lmadi:", e)


# ============ ERPNext O'QISH funksiyalari (tool'lar shu yerda bajariladi) =====
# Child table (qator) doctype'lari — bularni to'g'ridan-to'g'ri /resource orqali
# o'qib bo'lmaydi, frappe.client.get_list method orqali olamiz.
CHILD_DOCTYPES = {
    "Sales Invoice Item": "Sales Invoice",
    "Purchase Invoice Item": "Purchase Invoice",
    "Sales Order Item": "Sales Order",
    "Purchase Order Item": "Purchase Order",
    "Delivery Note Item": "Delivery Note",
    "Purchase Receipt Item": "Purchase Receipt",
}


def erp_list(doctype, filters=None, fields=None, limit=50, order_by=None):
    """Berilgan doctype bo'yicha hujjatlar ro'yxatini oladi (faqat o'qish).
    Child table bo'lsa frappe.client.get_list method orqali oladi."""
    if doctype in CHILD_DOCTYPES:
        # child table — method API orqali, parent maydonini ham qo'shamiz
        f = list(fields or ["name"])
        if "parent" not in f:
            f.append("parent")
        params = {
            "doctype": doctype,
            "filters": json.dumps(filters or []),
            "fields": json.dumps(f),
            "limit_page_length": limit,
            "parent": CHILD_DOCTYPES[doctype],
        }
        if order_by:
            params["order_by"] = order_by
        r = requests.get(f"{ERP_URL}/api/method/frappe.client.get_list",
                         headers=ERP_HEADERS, params=params, timeout=90)
        r.raise_for_status()
        return r.json().get("message", [])

    params = {
        "filters": json.dumps(filters or []),
        "fields": json.dumps(fields or ["name"]),
        "limit_page_length": limit,
    }
    if order_by:
        params["order_by"] = order_by
    r = requests.get(f"{ERP_URL}/api/resource/{doctype}",
                     headers=ERP_HEADERS, params=params, timeout=60)
    r.raise_for_status()
    return r.json().get("data", [])


def erp_get_doc(doctype, name):
    """Bitta hujjatning to'liq ma'lumotini oladi."""
    r = requests.get(f"{ERP_URL}/api/resource/{doctype}/{name}",
                     headers=ERP_HEADERS, timeout=60)
    r.raise_for_status()
    return r.json().get("data", {})


def erp_aggregate(doctype, group_by, metric="sum", field="base_grand_total",
                  filters=None, order="desc", limit=50):
    """
    Guruhlash + yig'ish (server tomonida hisoblanadi — kam token, aniq natija).
    Masalan: mijoz bo'yicha umumiy sotuv => doctype="Sales Invoice",
             group_by="customer", metric="sum", field="base_grand_total".
    metric: sum | count | avg | max | min
    """
    expr = "count(name)" if metric == "count" else f"{metric}(`{field}`)"
    params = {
        "fields": json.dumps([group_by, f"{expr} as value"]),
        "filters": json.dumps(filters or []),
        "group_by": group_by,
        "order_by": f"{expr} {order}",
        "limit_page_length": limit,
    }
    r = requests.get(f"{ERP_URL}/api/resource/{doctype}",
                     headers=ERP_HEADERS, params=params, timeout=90)
    r.raise_for_status()
    return r.json().get("data", [])


def _run_query_report(report_name, filters=None):
    """Past darajali: query report'ni ishga tushirib, xom 'message' ni qaytaradi.
    ignore_prepared_report=1 — 'Prepared Report' qilib sozlangan hisobotlarni
    (masalan Balance Sheet) fonda kutmasdan, DARHOL sinxron hisoblashga majbur qiladi."""
    r = requests.get(f"{ERP_URL}/api/method/frappe.desk.query_report.run",
                     headers=ERP_HEADERS,
                     params={"report_name": report_name,
                             "filters": json.dumps(filters or {}),
                             "ignore_prepared_report": 1},
                     timeout=180)
    r.raise_for_status()
    return r.json().get("message", {})


def _rows_as_dicts(message):
    """Hisobot qatorlarini (dict yoki list bo'lishidan qat'i nazar) dict ro'yxatiga aylantiradi."""
    fieldnames = []
    for c in message.get("columns", []):
        if isinstance(c, dict):
            fieldnames.append(c.get("fieldname") or c.get("label"))
        else:
            fieldnames.append(str(c).split(":")[0].strip())
    out = []
    for row in message.get("result", []):
        if isinstance(row, dict):
            out.append(row)
        elif isinstance(row, (list, tuple)):
            out.append({fieldnames[i]: row[i] for i in range(min(len(fieldnames), len(row)))})
    return out


def erp_run_report(report_name, filters=None):
    """ERPNext tayyor hisobotini ishga tushiradi (masalan 'Gross Profit' — real foyda)."""
    msg = _run_query_report(report_name, filters)
    cols = []
    for c in msg.get("columns", []):
        cols.append(c.get("label") or c.get("fieldname") if isinstance(c, dict) else str(c).split(":")[0])
    return {"columns": cols, "rows": msg.get("result", [])[:200]}


def erp_profit_breakdown(from_date, to_date, by="item_code", customer=None, top=20, company=None):
    """
    Real FOYDANI mijoz/tovar kesimida hisoblaydi.
    'Gross Profit' hisobotini Invoice darajasida olib (har qatorda customer+item+gross_profit),
    so'ng Python ichida qayta yig'adi. Shu sababli 'falon mijozning qaysi tovari ko'p foyda
    keltiradi' degan ikki bosqichli savolga ham javob bera oladi.
      by = "item_code"  -> tovarlar bo'yicha
           "customer"   -> mijozlar bo'yicha
           "customer_item" -> mijoz+tovar juftligi bo'yicha
      customer = faqat shu mijoz bilan cheklash (ixtiyoriy)
    """
    comp = company or COMPANY
    if not comp:
        return {"error": "Bu hisobot uchun 'company' kerak. SOZLAMALAR'dagi COMPANY ni to'ldiring "
                         "yoki so'rovda kompaniya nomini ayting."}
    msg = _run_query_report("Gross Profit", {
        "company": comp, "from_date": from_date, "to_date": to_date, "group_by": "Invoice",
    })
    rows = _rows_as_dicts(msg)

    cur_customer = None       # invoice sarlavhasidagi mijozni keyingi tovar qatorlariga tarqatamiz
    agg = {}
    for r in rows:
        if r.get("customer"):
            cur_customer = r.get("customer")
        item = r.get("item_code")
        gp = r.get("gross_profit")
        if not item or gp in (None, ""):
            continue                      # subtotal/sarlavha qatorini o'tkazib yuboramiz
        cust = r.get("customer") or cur_customer
        if customer and cust != customer:
            continue
        if by == "customer":
            key = cust
        elif by == "customer_item":
            key = f"{cust} | {item}"
        else:
            key = item
        a = agg.setdefault(key, {"gross_profit": 0.0, "revenue": 0.0, "qty": 0.0})
        a["gross_profit"] += float(gp or 0)
        a["revenue"] += float(r.get("base_amount") or 0)
        a["qty"] += float(r.get("qty") or 0)

    ranked = sorted(agg.items(), key=lambda kv: kv[1]["gross_profit"], reverse=True)[:top]
    if not ranked:
        return {"warning": "Foyda ma'lumoti topilmadi. Ehtimol tovar tannarxi (valuation) "
                           "kiritilmagan yoki bu davrda sotuv yo'q.", "items": []}
    return {"by": by, "customer": customer,
            "items": [dict(key=k, **v) for k, v in ranked]}


# ============ HISOBOTNI FAYL (Excel/CSV/PDF) qilib yuborish ===================
def _report_table(report_name, filters=None):
    """Hisobotni ishga tushirib, (sarlavhalar, qatorlar matritsasi) ko'rinishida qaytaradi.
    Moliyaviy hisobotlardagi 'indent' (ichki bandlar) bo'lsa, hisob nomiga bo'sh joy qo'shadi."""
    msg = _run_query_report(report_name, filters)
    columns = msg.get("columns", [])
    headers, fieldnames = [], []
    for c in columns:
        if isinstance(c, dict):
            fn = c.get("fieldname") or c.get("label") or ""
            headers.append(c.get("label") or fn)
            fieldnames.append(fn)
        else:
            label = str(c).split(":")[0].strip()
            headers.append(label)
            fieldnames.append(label)

    # qaysi ustun "nomi" ustuni (indentni shunga qo'llaymiz)
    name_fields = ("account", "account_name", "party", "item", "label")
    matrix = []
    for row in msg.get("result", []):
        if isinstance(row, dict):
            indent = int(row.get("indent") or 0)
            line = []
            for fn in fieldnames:
                v = row.get(fn, "")
                if indent and fn in name_fields and isinstance(v, str) and v:
                    v = ("    " * indent) + v
                line.append(v)
            matrix.append(line)
        elif isinstance(row, (list, tuple)):
            matrix.append([row[i] if i < len(row) else "" for i in range(len(fieldnames))])
        # None (ajratuvchi) qatorlarni tashlab yuboramiz
    return headers, matrix


def _build_xlsx(headers, matrix, sheet_title="Hisobot"):
    """openpyxl bilan .xlsx bayt-massivini yasaydi."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = (sheet_title or "Hisobot")[:31]
    ws.append([str(h) for h in headers])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in matrix:
        ws.append([_clean_cell(v) for v in row])
    # ustun kengligini sarlavha bo'yicha taxminan moslaymiz
    for i, h in enumerate(headers, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = max(12, min(40, len(str(h)) + 4))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _clean_cell(v):
    """Excel katakchasi uchun qiymatni tozalaydi (raqam — raqam, qolgani matn)."""
    if v is None:
        return ""
    if isinstance(v, (int, float, str)):
        return v
    return str(v)


def _build_csv(headers, matrix):
    """CSV bayt-massivi (Excel to'g'ri ochishi uchun UTF-8 BOM bilan)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([str(h) for h in headers])
    for row in matrix:
        w.writerow(["" if v is None else v for v in row])
    return buf.getvalue().encode("utf-8-sig")


def _report_html(title, headers, matrix):
    """Hisobotdan sodda, chiroyli HTML jadval yasaydi (PDF uchun)."""
    def esc(x):
        return (str(x) if x is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    th = "".join(f"<th>{esc(h)}</th>" for h in headers)
    trs = []
    for row in matrix:
        tds = "".join(f"<td>{esc(v)}</td>" for v in row)
        trs.append(f"<tr>{tds}</tr>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
        body{{font-family:Arial,sans-serif;font-size:11px;color:#222}}
        h2{{margin:0 0 4px}} .sub{{color:#666;font-size:10px;margin-bottom:10px}}
        table{{border-collapse:collapse;width:100%}}
        th,td{{border:1px solid #ccc;padding:4px 6px;text-align:left}}
        th{{background:#f2f2f2}} td:not(:first-child){{text-align:right}}
        </style></head><body>
        <h2>{esc(title)}</h2>
        <div class="sub">{esc(COMPANY)} · {esc(title)}</div>
        <table><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>
        </body></html>"""


def _build_pdf(title, headers, matrix, orientation="Landscape"):
    """ERPNext'ning o'z PDF generatori (wkhtmltopdf) orqali PDF yasaydi.
    Mahalliy PDF kutubxonasi shart emas. Muvaffaqiyatsiz bo'lsa exception tashlaydi."""
    html = _report_html(title, headers, matrix)
    r = requests.post(f"{ERP_URL}/api/method/frappe.utils.print_format.report_to_pdf",
                      headers=ERP_HEADERS,
                      data={"html": html, "orientation": orientation},
                      timeout=120)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if "pdf" not in ctype and not r.content[:4] == b"%PDF":
        raise RuntimeError(f"PDF o'rniga boshqa javob keldi ({ctype}): {r.text[:200]}")
    return r.content


# Moliyaviy hisobotlar uchun MAJBURIY (sukutdagi) filtrlar.
# Foydalanuvchi faqat sana oralig'ini beradi; qolganini bot avtomatik to'ldiradi.
#   Balance Sheet            -> Date Range, Monthly, accumulated_values YOQIQ
#   Profit and Loss Statement-> Date Range, Monthly, accumulated_values O'CHIQ
#   Cash Flow                -> Date Range, Monthly
FIN_REPORT_DEFAULTS = {
    "Balance Sheet": {"filter_based_on": "Date Range", "periodicity": "Monthly", "accumulated_values": 1},
    "Profit and Loss Statement": {"filter_based_on": "Date Range", "periodicity": "Monthly", "accumulated_values": 0},
    "Cash Flow": {"filter_based_on": "Date Range", "periodicity": "Monthly"},
}

# Foydalanuvchi/Claude turlicha atashi mumkin — kanonik ERPNext nomiga keltiramiz.
REPORT_ALIASES = {
    "balance sheet": "Balance Sheet",
    "balans": "Balance Sheet",
    "balanssheet": "Balance Sheet",
    "profit and loss": "Profit and Loss Statement",
    "profit and loss statement": "Profit and Loss Statement",
    "p&l": "Profit and Loss Statement",
    "pnl": "Profit and Loss Statement",
    "foyda va zarar": "Profit and Loss Statement",
    "foyda zarar": "Profit and Loss Statement",
    "cash flow": "Cash Flow",
    "cash flow statement": "Cash Flow",
    "pul oqimi": "Cash Flow",
}


def _canonical_report_name(name):
    # chiziqcha/ortiqcha bo'shliqlarni bo'sh joyga keltirib, alias'ni bardoshli izlaymiz
    key = " ".join((name or "").replace("-", " ").split()).lower()
    return REPORT_ALIASES.get(key, name)


def _apply_fin_defaults(report_name, filters):
    """Moliyaviy hisobot uchun majburiy filtrlarni qo'llaydi. Sana (period_start_date/
    period_end_date) foydalanuvchidan keladi, qolgan texnik filtrlar avtomatik."""
    f = dict(filters or {})
    f.setdefault("company", COMPANY)
    defaults = FIN_REPORT_DEFAULTS.get(report_name)
    if defaults:
        f.update(defaults)   # bu sozlamalar MAJBURIY (P&L da accumulated o'chiq bo'lib qoladi, h.k.)
    return f


def _report_data_text(report_name, headers, matrix, period=None,
                      limit_rows=300, limit_chars=45000):
    """Hisobot jadvalini Claude tahlil qila oladigan ixcham JSON-matnga aylantiradi."""
    payload = {
        "report": report_name,
        "period": period,
        "columns": [str(h) for h in headers],
        "rows": [[_clean_cell(v) for v in row] for row in matrix[:limit_rows]],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)[:limit_chars]


def get_report_data(args):
    """Hisobot ma'lumotini FAYL yubormasdan matn ko'rinishida qaytaradi (tahlil uchun)."""
    report_name = _canonical_report_name(args["report_name"])
    filters = _apply_fin_defaults(report_name, args.get("filters"))
    if report_name in FIN_REPORT_DEFAULTS and not (filters.get("period_start_date") and filters.get("period_end_date")):
        return ("Bu hisobot uchun sana oralig'i kerak. period_start_date va "
                "period_end_date (YYYY-MM-DD) ber.")
    headers, matrix = _report_table(report_name, filters)
    if not matrix:
        return "Hisobot bo'sh — ma'lumot topilmadi. Filtrlar/sanani tekshiring."
    period = f"{filters.get('period_start_date')} .. {filters.get('period_end_date')}"
    return _report_data_text(report_name, headers, matrix, period)


def send_report_file(args, chat_id):
    """ERPNext hisobotini fayl qilib (xlsx/csv/pdf) Telegram'ga yuboradi.
    Claude shu tool orqali foydalanuvchiga haqiqiy fayl jo'nata oladi."""
    if chat_id is None:
        return "Fayl yuborib bo'lmadi: chat aniqlanmadi."
    report_name = _canonical_report_name(args["report_name"])
    filters = _apply_fin_defaults(report_name, args.get("filters"))
    if report_name in FIN_REPORT_DEFAULTS and not (filters.get("period_start_date") and filters.get("period_end_date")):
        return ("Bu hisobot uchun sana oralig'i kerak. Foydalanuvchidan qaysi davr ekanini "
                "aniqlab, filters ichida period_start_date va period_end_date (YYYY-MM-DD) ber.")
    fmt = (args.get("file_format") or "xlsx").lower()
    title = args.get("title") or report_name
    base = (args.get("filename") or report_name).replace(" ", "_").replace("/", "-")

    headers, matrix = _report_table(report_name, filters)
    if not matrix:
        return ("Hisobot bo'sh — yuboradigan ma'lumot topilmadi. "
                "Filtrlarni (sana/kompaniya) tekshiring.")

    caption = args.get("caption") or title
    try:
        if fmt == "csv":
            content, fname = _build_csv(headers, matrix), f"{base}.csv"
        elif fmt == "pdf":
            content, fname = _build_pdf(title, headers, matrix), f"{base}.pdf"
        else:
            content, fname = _build_xlsx(headers, matrix, report_name), f"{base}.xlsx"
    except Exception as e:
        # PDF/Excel yasashda muammo bo'lsa — CSV ga qaytamiz, baribir fayl ketsin
        content, fname = _build_csv(headers, matrix), f"{base}.csv"
        tg_send_document(chat_id, fname, content, caption=caption)
        return (f"'{fmt}' formatini yasab bo'lmadi ({e}). Buning o'rniga CSV yubordim: "
                f"{fname} ({len(matrix)} qator).")

    tg_send_document(chat_id, fname, content, caption=caption)
    period = f"{filters.get('period_start_date')} .. {filters.get('period_end_date')}"
    data_text = _report_data_text(report_name, headers, matrix, period)
    return (f"Fayl yuborildi: {fname} ({len(matrix)} qator, {len(headers)} ustun).\n"
            f"Shu hisobot ma'lumoti (kerak bo'lsa tahlil uchun):\n{data_text}")


def run_tool(name, args, chat_id=None):
    """Claude chaqirgan tool'ni bajarib, natijani matn ko'rinishida qaytaradi."""
    try:
        if name == "send_report_file":
            return send_report_file(args, chat_id)
        if name == "get_report_data":
            return get_report_data(args)
        if name == "erp_list":
            data = erp_list(
                args["doctype"],
                args.get("filters"),
                args.get("fields"),
                args.get("limit", 50),
                args.get("order_by"),
            )
            return json.dumps(data, ensure_ascii=False, default=str)
        elif name == "erp_get_doc":
            data = erp_get_doc(args["doctype"], args["name"])
            return json.dumps(data, ensure_ascii=False, default=str)
        elif name == "erp_aggregate":
            data = erp_aggregate(
                args["doctype"], args["group_by"],
                args.get("metric", "sum"),
                args.get("field", "base_grand_total"),
                args.get("filters"),
                args.get("order", "desc"),
                args.get("limit", 50),
            )
            return json.dumps(data, ensure_ascii=False, default=str)
        elif name == "erp_run_report":
            data = erp_run_report(args["report_name"], args.get("filters"))
            return json.dumps(data, ensure_ascii=False, default=str)
        elif name == "erp_profit_breakdown":
            data = erp_profit_breakdown(
                args["from_date"], args["to_date"],
                args.get("by", "item_code"),
                args.get("customer"),
                args.get("top", 20),
                args.get("company"),
            )
            return json.dumps(data, ensure_ascii=False, default=str)
        else:
            return f"Noma'lum tool: {name}"
    except Exception as e:
        return f"ERP xatosi: {e}"


# ============ Claude uchun tool ta'riflari =====================================
TOOLS = [
    {
        "name": "erp_list",
        "description": (
            "ERPNext'dan hujjatlar ro'yxatini o'qiydi (faqat o'qish). "
            "Filtr formati: [[\"maydon\",\"operator\",\"qiymat\"], ...]. "
            "Operatorlar: =, !=, >, <, >=, <=, like, in, between, is. "
            "Sana maydonlari: posting_date, creation, modified. "
            "Kim yaratgani: owner (email). Kim o'zgartirgani: modified_by."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doctype": {"type": "string",
                            "description": "Masalan: Sales Invoice, Delivery Note, Purchase Receipt, Purchase Invoice, Payment Entry"},
                "filters": {"type": "array", "items": {"type": "array"},
                            "description": "Filtrlar ro'yxati"},
                "fields": {"type": "array", "items": {"type": "string"},
                           "description": "Olinadigan maydonlar, masalan [\"name\",\"customer\",\"grand_total\",\"owner\",\"creation\"]"},
                "limit": {"type": "integer", "description": "Maksimal qatorlar (default 50)"},
                "order_by": {"type": "string", "description": "Masalan: 'creation desc'"},
            },
            "required": ["doctype"],
        },
    },
    {
        "name": "erp_get_doc",
        "description": "Bitta ERPNext hujjatining to'liq tafsilotini o'qiydi (faqat o'qish).",
        "input_schema": {
            "type": "object",
            "properties": {
                "doctype": {"type": "string"},
                "name": {"type": "string", "description": "Hujjat ID/nomi"},
            },
            "required": ["doctype", "name"],
        },
    },
    {
        "name": "erp_aggregate",
        "description": (
            "Hujjatlarni biror maydon bo'yicha GURUHLAB, yig'indi/son/o'rtachasini "
            "hisoblaydi (server tomonida — tez va aniq). Tahlil savollari uchun shuni ishlat. "
            "Misol: mijoz bo'yicha bu oygi umumiy sotuv => doctype='Sales Invoice', "
            "group_by='customer', metric='sum', field='base_grand_total', "
            "filters=[['docstatus','=',1],['posting_date','between',['2026-05-01','2026-05-31']]]. "
            "Oylik trendni topish uchun har oy uchun alohida chaqir (sana filtrini o'zgartirib)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doctype": {"type": "string"},
                "group_by": {"type": "string", "description": "Guruhlash maydoni, masalan 'customer', 'supplier', 'item_code'"},
                "metric": {"type": "string", "enum": ["sum", "count", "avg", "max", "min"]},
                "field": {"type": "string", "description": "Hisoblanadigan maydon, masalan 'base_grand_total' (count uchun kerak emas)"},
                "filters": {"type": "array", "items": {"type": "array"}},
                "order": {"type": "string", "enum": ["desc", "asc"]},
                "limit": {"type": "integer"},
            },
            "required": ["doctype", "group_by"],
        },
    },
    {
        "name": "erp_run_report",
        "description": (
            "ERPNext'ning tayyor hisobotini ishga tushiradi. REAL FOYDA uchun "
            "report_name='Gross Profit' ishlat (mijoz bo'yicha foyda: filters ichida "
            "{'group_by':'Customer','company':..,'from_date':..,'to_date':..}). "
            "DIQQAT: foyda faqat ERPNext'da tovar tannarxi kiritilgan bo'lsa to'g'ri chiqadi."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_name": {"type": "string"},
                "filters": {"type": "object", "description": "Hisobot filtrlari (obyekt ko'rinishida)"},
            },
            "required": ["report_name"],
        },
    },
    {
        "name": "erp_profit_breakdown",
        "description": (
            "Real FOYDANI mijoz va/yoki tovar kesimida hisoblaydi. Standart Gross Profit "
            "hisoboti bera olmaydigan IKKI BOSQICHLI savol uchun: 'falon mijozning qaysi "
            "tovaridan ko'proq foyda olyapmiz'. Buning uchun customer='Mijoz nomi' va "
            "by='item_code' ber. Boshqa rejimlar: by='customer' (mijozlar reytingi), "
            "by='customer_item' (mijoz+tovar juftliklari). Sana oralig'i shart."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "YYYY-MM-DD"},
                "by": {"type": "string", "enum": ["item_code", "customer", "customer_item"]},
                "customer": {"type": "string", "description": "Faqat shu mijoz bilan cheklash (ixtiyoriy)"},
                "top": {"type": "integer", "description": "Nechta natija (default 20)"},
                "company": {"type": "string", "description": "Kompaniya nomi (sozlamada bo'lsa shart emas)"},
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_report_data",
        "description": (
            "ERPNext hisobotining RAQAMLARINI o'qiydi (fayl yubormaydi). Hisobotni TAHLIL "
            "qilish, izohlash yoki maslahat berish uchun SHUNI ishlat — raqamlar senga matn "
            "bo'lib qaytadi. report_name: 'Balance Sheet', 'Profit and Loss Statement', 'Cash Flow' "
            "(va boshqalar). Bu 3 hisobot uchun filters'ga faqat period_start_date va "
            "period_end_date (YYYY-MM-DD) ber — qolganini bot avtomatik qo'yadi."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_name": {"type": "string"},
                "filters": {"type": "object", "description": "period_start_date, period_end_date va h.k."},
            },
            "required": ["report_name"],
        },
    },
    {
        "name": "send_report_file",
        "description": (
            "ERPNext hisobotini HAQIQIY FAYL (Excel/CSV/PDF) qilib foydalanuvchiga "
            "Telegram orqali yuboradi. Foydalanuvchi 'PDF qilib jo'nat', 'Excel/fayl "
            "qilib ber', 'yuklab ber' desa SHU tool'ni ishlat — matn jadval emas, fayl yubor. "
            "report_name — ERPNext hisobot nomi (masalan: 'Balance Sheet', 'Profit and Loss "
            "Statement', 'General Ledger', 'Accounts Receivable', 'Trial Balance'). "
            "file_format: 'pdf' (chiroyli, chop etishga tayyor), 'xlsx' (Excel, tahrirlash uchun) "
            "yoki 'csv'. Foydalanuvchi formatni aytmasa: PDF so'ralganda 'pdf', aks holda 'xlsx'. "
            "filters — hisobot filtrlari OBYEKT ko'rinishida.\n"
            "MUHIM: Balance Sheet, 'Profit and Loss Statement', 'Cash Flow' uchun SEN faqat "
            "filters'ga period_start_date va period_end_date (YYYY-MM-DD) ber. filter_based_on, "
            "periodicity (Monthly), accumulated_values ni bot O'ZI to'g'ri qo'yadi — ularni yozma. "
            "Sana berilmasa foydalanuvchidan qaysi davr ekanini avval SO'RA."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_name": {"type": "string", "description": "ERPNext hisobot nomi"},
                "filters": {"type": "object", "description": "Hisobot filtrlari (obyekt)"},
                "file_format": {"type": "string", "enum": ["pdf", "xlsx", "csv"]},
                "filename": {"type": "string", "description": "Fayl nomi (kengaytmasiz, ixtiyoriy)"},
                "title": {"type": "string", "description": "Fayl ichidagi sarlavha (ixtiyoriy)"},
                "caption": {"type": "string", "description": "Telegram'dagi fayl izohi (ixtiyoriy)"},
            },
            "required": ["report_name"],
        },
    },
]

SYSTEM_PROMPT = f"""Sen — ERPNext bo'yicha o'zbek tilida javob beradigan yordamchi assistantsan.
Foydalanuvchi savol bersa, kerakli ma'lumotni erp_list yoki erp_get_doc tool'lari
orqali ERPNext'dan olib, qisqa va aniq javob ber. Raqamlarni o'qiladigan qilib yoz.

Foydali bilimlar:
- Faqat tasdiqlangan hujjatlar uchun filtrga ["docstatus","=",1] qo'sh.
- "Invoys qilinmagan Delivery Note" = Delivery Note, status="To Bill".
- "Invoys qilinmagan Purchase Receipt" = Purchase Receipt, status="To Bill".
- "Kim kiritgan/yaratgan" = 'owner' maydoni (email). 'creation' = yaratilgan vaqt.
- Naqd kassa = Payment Entry, mode_of_payment="Cash" (kompaniyada nomi boshqa bo'lishi mumkin).
- Kompaniya filtri: { '["company","=","'+COMPANY+'"]' if COMPANY else 'kerak emas (hamma kompaniya)' }.
- Sanani 'YYYY-MM-DD' formatida ishlat. Oraliq uchun 'between' operatori: ["posting_date","between",["2026-05-01","2026-05-31"]].

TAHLIL SAVOLLARI uchun:
- "Kim ko'proq oldi / eng yaxshi mijozlar" => erp_aggregate, doctype="Sales Invoice",
  group_by="customer", metric="sum", field="base_grand_total", sana filtri bilan.
- "Falon mijoz bu oy kam oldimi?" => o'sha mijoz uchun har oy (oxirgi 3 oy) alohida
  erp_aggregate chaqir (filters: customer + posting_date between), keyin oylarni solishtir.
- "Qaysi tovar ko'p sotilyapti / qaysi tovardan ko'p foyda" => erp_profit_breakdown,
  by="item_code" ishlat (sana oralig'i bilan). DIQQAT: "Sales Invoice Item" yoki boshqa
  "... Item" child table'ni erp_aggregate bilan GURUHLAMA — u ishlamaydi. Tovar darajasidagi
  tahlil uchun DOIM erp_profit_breakdown ishlat.
- REAL FOYDA so'ralsa => erp_run_report("Gross Profit") yoki erp_profit_breakdown.
  Agar tannarx kiritilmagan bo'lsa yoki hisobot bo'sh kelsa — buni foydalanuvchiga tushuntir
  va tushum (revenue) bo'yicha tahlil taklif qil. Tushum ≠ foyda ekanini aniq ayt.
- "Falon MIJOZNING qaysi TOVARI ko'proq foyda keltiradi" (ikki bosqichli) => 
  erp_profit_breakdown, customer='Mijoz', by='item_code', sana oralig'i bilan.
  Mijozlar reytingi uchun by='customer'.
- Tahlil qilganda faqat raqamlarni emas, qisqa xulosa ham ber (masalan "X mijoz mayda
  oldi: aprelda 50 mln, mayda 18 mln — 64% pasaygan").

FAYL (PDF/Excel) YUBORISH:
- Foydalanuvchi hisobotni "PDF qilib jo'nat", "Excel/fayl qilib ber", "yuklab ber" desa —
  send_report_file tool'ini ishlat. SEN FAYL YUBORA OLASAN — "imkonim yo'q" DEMA.
- 3 ta asosiy moliyaviy hisobot: report_name="Balance Sheet" (balans),
  "Profit and Loss Statement" (foyda-zarar), "Cash Flow" (pul oqimi).
- Bu 3 hisobot uchun SEN faqat sana berasan — filters'ga period_start_date va
  period_end_date (YYYY-MM-DD). filter_based_on/periodicity/accumulated ni bot o'zi qo'yadi.
    * Balance Sheet "as on" hisobot: period_start_date = moliyaviy yil boshi (2026-01-01),
      period_end_date = so'ralgan sana (masalan 2026-04-30).
    * Profit and Loss / Cash Flow: period_start_date va period_end_date = so'ralgan davr
      boshi va oxiri (masalan "mart-may" => 2026-03-01 ... 2026-05-31; "aprel" => 2026-04-01 ... 2026-04-30).
- Format aytilmasa: "PDF" so'ralsa file_format="pdf", aks holda "xlsx" (Excel).
- Sana berilmasa, foydalanuvchidan qaysi davr ekanini avval SO'RA.
- Fayl ketgach qisqa tasdiq yoz (masalan "Balance Sheet PDF faylini yubordim ✅").

HISOBOTNI TAHLIL QILISH (CFO / moliyachi sifatida):
- Foydalanuvchi "tahlil qil", "CFO/moliyachi sifatida bahola", "nima qilish kerak",
  "izohlab ber" desa — kerakli hisobot raqamlarini get_report_data bilan O'ZING ol va tahlil qil.
- HECH QACHON foydalanuvchidan raqamlarni qo'lda kiritishini SO'RAMA. "PDF/Excel faylni
  o'qiy olmayman" deb uzr ham so'rama — ma'lumot senda get_report_data orqali bor.
  (Fayl yuborgan bo'lsang, uning ma'lumoti tool natijasida senga allaqachon qaytgan.)
- Tahlilda quyidagilarni yorit: asosiy ko'rsatkichlar (aktiv/passiv/sarmoya yoki
  tushum/xarajat/sof foyda yoki sof pul oqimi), muhim nisbatlar (joriy likvidlik,
  foyda marjasi, qarz/sarmoya), oylar bo'yicha TREND (o'sish/pasayish %), e'tibor
  beriladigan xavflar, va 3-5 ta ANIQ amaliy tavsiya. Raqamlarni o'qiladigan qil.
- Tahlil uchun bir nechta hisobotni birga olib solishtirsang bo'ladi (masalan P&L + Cash Flow).

Hech qachon ma'lumotni o'zingdan to'qima — faqat tool natijasiga asoslanib javob ber.
Agar tool xato qaytarsa, foydalanuvchiga sodda tilda tushuntir."""


# ============ Claude API bilan agentik tsikl =================================
# Qayta urinish sozlamalari (rate limit / vaqtinchalik xatolar uchun)
ANTHROPIC_MAX_RETRIES = 6     # ko'pi bilan necha marta qayta urinish
ANTHROPIC_BACKOFF_CAP = 60    # ikki urinish orasidagi maksimal kutish (soniya)


def _anthropic_post(payload):
    """Anthropic'ga POST yuboradi. 429 (rate limit), 529 (overloaded) va 5xx
    xatolarida avtomatik qayta uradi — Anthropic bergan 'retry-after' sarlavhasini
    hisobga olib, eksponensial backoff bilan. Foydalanuvchi xatoni ko'rmaydi."""
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    last_exc = None
    for attempt in range(ANTHROPIC_MAX_RETRIES + 1):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers, json=payload, timeout=120,
            )
            # Qayta urinsa bo'ladigan xatolar: 429 va 5xx (529 ham shu ichida)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < ANTHROPIC_MAX_RETRIES:
                    wait = _retry_wait(r, attempt)
                    print(f"Anthropic {r.status_code} — {wait:.0f}s kutib qayta urinaman "
                          f"({attempt + 1}/{ANTHROPIC_MAX_RETRIES})")
                    time.sleep(wait)
                    continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            # Tarmoq/timeout xatosi — bularni ham qayta urinamiz
            last_exc = e
            if attempt < ANTHROPIC_MAX_RETRIES:
                wait = min(ANTHROPIC_BACKOFF_CAP, 2 ** attempt)
                print(f"Anthropic tarmoq xatosi ({e}) — {wait}s kutib qayta urinaman "
                      f"({attempt + 1}/{ANTHROPIC_MAX_RETRIES})")
                time.sleep(wait)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("Anthropic'ga ulanib bo'lmadi (qayta urinishlar tugadi).")


def _retry_wait(resp, attempt):
    """Keyingi urinishgacha necha soniya kutishni hisoblaydi.
    Avval server bergan 'retry-after' sarlavhasiga ishonamiz, bo'lmasa
    eksponensial backoff (2,4,8,...) — maksimal ANTHROPIC_BACKOFF_CAP gacha."""
    ra = resp.headers.get("retry-after")
    if ra:
        try:
            return min(ANTHROPIC_BACKOFF_CAP, float(ra))
        except ValueError:
            pass
    return min(ANTHROPIC_BACKOFF_CAP, 2 ** attempt)


def ask_claude(history, chat_id=None):
    """history — Anthropic messages ro'yxati. Tool'larni bajarib, yakuniy javobni qaytaradi.
    chat_id — fayl yuboradigan tool'lar uchun (send_report_file)."""
    for _ in range(8):   # ko'pi bilan 8 marta tool chaqirishga ruxsat
        data = _anthropic_post({
            "model": MODEL,
            "max_tokens": 1500,
            "system": SYSTEM_PROMPT,
            "tools": TOOLS,
            "messages": history,
        })
        history.append({"role": "assistant", "content": data["content"]})

        if data.get("stop_reason") == "tool_use":
            # Claude bir yoki bir nechta tool chaqirdi — bajaramiz
            results = []
            for block in data["content"]:
                if block["type"] == "tool_use":
                    out = run_tool(block["name"], block["input"], chat_id)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": out[:50000],   # juda uzun natijani kesamiz
                    })
            history.append({"role": "user", "content": results})
            continue   # natija bilan Claude'ga qайта murojaat

        # Tool kerak emas — yakuniy matnli javob
        return "".join(b["text"] for b in data["content"] if b["type"] == "text")

    return "Kechirasiz, javobni tayyorlay olmadim (juda ko'p urinish)."


# ============ Telegram long-polling ==========================================
SESSIONS = {}   # chat_id -> messages tarixi (oddiy xotira; restartda o'chadi)
ERP_TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"


def tg_send(chat_id, text):
    for i in range(0, len(text), 4000):
        requests.post(f"{ERP_TG_API}/sendMessage",
                      data={"chat_id": chat_id, "text": text[i:i + 4000]}, timeout=30)


def tg_send_document(chat_id, filename, content, caption=None):
    """Telegram'ga fayl (hujjat) yuboradi. content — bayt-massiv."""
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
    requests.post(f"{ERP_TG_API}/sendDocument",
                  data=data,
                  files={"document": (filename, content)},
                  timeout=120)


def main():
    print("Bot ishga tushdi. To'xtatish: Ctrl+C")
    _autodetect_company()

    # Eski (bot o'chiq turgan paytdagi) xabarlarni tashlab yuboramiz —
    # qayta ishga tushganda eski savollarga javob bermasligi uchun.
    try:
        r0 = requests.get(f"{ERP_TG_API}/getUpdates",
                          params={"timeout": 0, "offset": -1}, timeout=20)
        res0 = r0.json().get("result", [])
        offset = (res0[-1]["update_id"] + 1) if res0 else None
    except Exception:
        offset = None

    seen = set()   # qayta ishlangan update_id'lar (ikki marta javobning oldini oladi)

    while True:
        try:
            resp = requests.get(f"{ERP_TG_API}/getUpdates",
                                params={"offset": offset, "timeout": 30}, timeout=40)
            updates = resp.json().get("result", [])
            for upd in updates:
                uid = upd["update_id"]
                # offset'ni DARHOL oldinga suramiz — Telegram bu update'ni qayta yubormaydi
                offset = uid + 1
                if uid in seen:
                    continue
                seen.add(uid)
                if len(seen) > 1000:
                    seen.clear()
                    seen.add(uid)

                msg = upd.get("message") or {}
                text = msg.get("text")
                chat_id = msg.get("chat", {}).get("id")
                user_id = msg.get("from", {}).get("id")
                if not text or chat_id is None:
                    continue

                # Xavfsizlik: faqat ruxsat etilgan foydalanuvchilar
                if user_id not in ALLOWED_USERS:
                    tg_send(chat_id, "Kechirasiz, sizga bu botdan foydalanishga ruxsat yo'q.")
                    continue

                if text.strip() in ("/start", "/help"):
                    tg_send(chat_id, "Salom! Men ERPNext yordamchingizman.\n"
                                     "Savol bering, masalan:\n"
                                     "• Kecha kim sales invoice kiritgan?\n"
                                     "• Invoys qilinmagan delivery note'lar qancha?\n"
                                     "• Bu oygi naqd kassa qancha?\n"
                                     "Suhbatni tozalash: /reset")
                    continue
                if text.strip() == "/reset":
                    SESSIONS.pop(chat_id, None)
                    tg_send(chat_id, "Suhbat tozalandi.")
                    continue

                tg_send(chat_id, "🔎 Tekshiryapman...")
                hist = SESSIONS.setdefault(chat_id, [])
                hist.append({"role": "user", "content": text})
                # tarixni juda uzun bo'lib ketmasligi uchun cheklaymiz
                if len(hist) > 20:
                    del hist[:len(hist) - 20]
                try:
                    answer = ask_claude(hist, chat_id)
                except Exception as e:
                    answer = f"Xatolik yuz berdi: {e}"
                tg_send(chat_id, answer)

        except KeyboardInterrupt:
            print("To'xtatildi.")
            break
        except Exception as e:
            print("Tsikl xatosi:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()