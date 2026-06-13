import os
import json
import shutil
import base64
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
import duckdb
import qrcode
from PIL import Image

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

QR_URL = "https://praphani.github.io/01-InteractiveCV/"
TODAY = date.today()

ROOT = Path(__file__).parent.parent
DOCS_DIR = ROOT / "docs"
SHARED_DIR = ROOT / "shared"


# ── helpers ──────────────────────────────────────────────────────────────────

def esc(s):
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def months_between(from_date, to_date):
    if to_date is None:
        to_date = TODAY
    months = (to_date.year - from_date.year) * 12 + (to_date.month - from_date.month)
    if to_date.day < from_date.day:
        months -= 1
    return max(0, months)


def fmt_tenure(months):
    yrs = months // 12
    mos = months % 12
    return yrs, mos


def tenure_html(yrs, mos):
    parts = []
    if yrs:
        parts.append(f"{yrs}<span>yr</span>")
    if mos:
        parts.append(f"{mos}<span>mo</span>")
    if not parts:
        parts.append("0<span>mo</span>")
    return " ".join(parts)


def parse_date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except ValueError:
            continue
    return None


def make_qr_b64():
    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(QR_URL)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#181D25", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ── Google Sheets auth ────────────────────────────────────────────────────────

def get_gc():
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        creds = Credentials.from_service_account_info(json.loads(sa_json), scopes=SCOPES)
    else:
        sa_file = Path(__file__).parent / "service_account.json"
        creds = Credentials.from_service_account_file(str(sa_file), scopes=SCOPES)
    return gspread.authorize(creds)


# ── fetch all sheets ──────────────────────────────────────────────────────────

def fetch_sheets(gc, sheet_id):
    print("  Fetching Google Sheets...")
    wb = gc.open_by_key(sheet_id)
    sheets = {}
    for ws in wb.worksheets():
        title = ws.title
        records = ws.get_all_records(numericise_ignore=["all"])
        sheets[title] = records
        print(f"    {title}: {len(records)} rows")
    return sheets


# ── build cv_data via DuckDB ──────────────────────────────────────────────────

def build_cv_data(sheets):
    print("  Building cv_data via DuckDB...")
    con = duckdb.connect()

    # Register each sheet as a DuckDB relation
    import pandas as pd
    for name, rows in sheets.items():
        if rows:
            df = pd.DataFrame(rows)
        else:
            df = pd.DataFrame()
        safe = name.replace(" ", "_").replace("-", "_")
        con.register(safe, df)

    # ── Person ────────────────────────────────────────────────────────────────
    person_rows = sheets.get("Person", [])
    if not person_rows:
        raise ValueError("Person sheet is empty")
    p = person_rows[0]

    loc_rows = sheets.get("Location", [])
    loc_map = {r["L_ID"]: r for r in loc_rows}

    person_loc = loc_map.get(p.get("L_ID", ""), {})
    city = person_loc.get("City", "")
    country = person_loc.get("Country", "")
    location = f"{city}, {country}" if city and country else city or country

    first = str(p.get("First Name", "")).strip()
    last = str(p.get("Last Name", "")).strip()
    full_name = f"{first} {last}".strip()
    initials = (first[0] if first else "") + (last[0] if last else "")

    summary_full = str(p.get("Career Summary", "")).strip()
    # Split into lede (first sentence) and rest
    dot_idx = summary_full.find(".")
    if dot_idx != -1:
        summary_lede = summary_full[: dot_idx + 1].strip()
        summary_rest = summary_full[dot_idx + 1 :].strip()
    else:
        summary_lede = summary_full
        summary_rest = ""

    person = {
        "name": full_name,
        "initials": initials,
        "role": str(p.get("Role", p.get("Career Summary", ""))).split(".")[0].strip(),
        "location": location,
        "country": country,
        "email": str(p.get("Email", "")).strip(),
        "phone": str(p.get("Phone", "")).strip(),
        "phone2": str(p.get("Phone 2", "")).strip(),
        "linkedin": str(p.get("linkedin", "")).strip(),
        "github": str(p.get("github", "")).strip(),
        "summary": summary_full,
        "summary_lede": summary_lede,
        "summary_rest": summary_rest,
        "motto": str(p.get("Motto", "")).strip(),
        "relocation": str(p.get("Relocation", "")).strip(),
    }

    # Fix: role should come from Role sheet (current role title), not Career Summary
    # We'll update this after processing roles below

    # ── Skills ────────────────────────────────────────────────────────────────
    skill_rows = sheets.get("Skills", [])
    skill_map = {r["S_ID"]: r for r in skill_rows}

    all_skills_list = [r["Skill Name"] for r in skill_rows if r.get("Skill Name")]
    key_skills_list = [
        r["Skill Name"]
        for r in skill_rows
        if str(r.get("Key Skills", "")).strip().upper() == "Y"
    ]

    # ── Task-Skills ───────────────────────────────────────────────────────────
    task_skill_rows = sheets.get("Task-Skills", [])
    task_skills_map = {}
    for ts in task_skill_rows:
        tid = ts.get("T_ID", "")
        sid = ts.get("S_ID", "")
        if tid and sid:
            seen = task_skills_map.setdefault(tid, [])
            if sid not in seen:
                seen.append(sid)

    # ── Tasks ─────────────────────────────────────────────────────────────────
    task_rows = sheets.get("Task", [])
    task_map = {}
    for t in task_rows:
        wid = t.get("W_ID", "")
        if wid:
            task_map.setdefault(wid, []).append(t)

    # ── Role-Location ─────────────────────────────────────────────────────────
    role_loc_rows = sheets.get("Role_Location", sheets.get("Role-Location", []))
    role_loc_map = {}
    for rl in role_loc_rows:
        wid = rl.get("W_ID", "")
        lid = rl.get("L_ID", "")
        if wid and lid:
            role_loc_map.setdefault(wid, []).append(lid)

    # ── Role-Transition ───────────────────────────────────────────────────────
    transition_rows = sheets.get("Role_Transition", sheets.get("Role-Transition", []))
    # Map: next_role_wid -> transition type
    transition_map = {}
    for tr in transition_rows:
        prev = tr.get("Previous Role (W_ID)", tr.get("Previous Role", ""))
        nxt = tr.get("Next Role (W_ID)", tr.get("Next Role", ""))
        ttype = tr.get("Transition Type", "")
        if nxt:
            transition_map[nxt] = ttype

    # ── Roles ─────────────────────────────────────────────────────────────────
    role_rows = sheets.get("Role", [])

    # Build role objects
    roles_by_org = {}
    current_role_title = None

    for r in role_rows:
        wid = r.get("W_ID", "")
        oid = r.get("O_ID", "")
        if not wid or not oid:
            continue

        from_date = parse_date(r.get("Work_From", ""))
        to_str = str(r.get("Work_To", "")).strip()
        to_date = parse_date(to_str) if to_str else None
        is_current = str(r.get("Current Flag", "")).strip().upper() == "Y"
        if is_current:
            to_date = None

        if from_date is None:
            continue

        total_months = months_between(from_date, to_date)
        yrs, mos = fmt_tenure(total_months)

        # Locations for this role (Bangkok first)
        loc_ids = role_loc_map.get(wid, [])
        locs = []
        bangkok_locs = []
        other_locs = []
        for lid in loc_ids:
            ldata = loc_map.get(lid, {})
            city_name = ldata.get("City", "")
            if city_name.lower() == "bangkok":
                bangkok_locs.append(city_name)
            else:
                other_locs.append(city_name)
        locs = bangkok_locs + other_locs

        # Skills for this role (union of all task skills)
        role_task_rows = sorted(
            task_map.get(wid, []),
            key=lambda t: int(t.get("Task_Sequence", t.get("Seq", 99)) or 99),
        )

        role_skill_ids = set()
        tasks_out = []
        for t in role_task_rows:
            tid = t.get("T_ID", "")
            task_sids = task_skills_map.get(tid, [])
            role_skill_ids.update(task_sids)

            skills_for_task = []
            for sid in task_sids:
                sdata = skill_map.get(sid, {})
                sname = sdata.get("Skill Name", "")
                scat = sdata.get("Skill Category", "")
                if sname:
                    skills_for_task.append({"name": sname, "category": scat})

            tasks_out.append({
                "id": tid,
                "seq": int(t.get("Task_Sequence", t.get("Seq", 99)) or 99),
                "name": str(t.get("Task Name", "")).strip(),
                "description": str(t.get("Task Description", "")).strip(),
                "challenge": str(t.get("Challenge", "")).strip(),
                "input": str(t.get("Input", "")).strip(),
                "output": str(t.get("Output", "")).strip(),
                "outcome": str(t.get("Outcome", "")).strip(),
                "skills": skills_for_task,
            })

        role_all_skills = []
        for sid in role_skill_ids:
            sdata = skill_map.get(sid, {})
            sname = sdata.get("Skill Name", "")
            scat = sdata.get("Skill Category", "")
            if sname:
                role_all_skills.append({"name": sname, "category": scat})
        role_all_skills.sort(key=lambda s: s["name"])

        if is_current and current_role_title is None:
            current_role_title = str(r.get("Role", "")).strip()

        role_obj = {
            "id": wid,
            "oid": oid,
            "role": str(r.get("Role", "")).strip(),
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat() if to_date else None,
            "is_current": is_current,
            "tenure_years": yrs,
            "tenure_months": mos,
            "locations": locs,
            "all_skills": role_all_skills,
            "tasks": tasks_out,
            "_from_date_obj": from_date,
            "_analytics": str(r.get("Analytics", "")).strip().upper() == "Y",
            "_consulting": str(r.get("Consulting", "")).strip().upper() == "Y",
            "_total_months": total_months,
        }

        roles_by_org.setdefault(oid, []).append(role_obj)

    if current_role_title:
        person["role"] = current_role_title

    # ── Organizations ─────────────────────────────────────────────────────────
    org_rows = sheets.get("Organization", [])
    org_map = {r["O_ID"]: r for r in org_rows}

    # ── Companies (grouped roles) ─────────────────────────────────────────────
    companies = []
    for oid, org_roles in roles_by_org.items():
        org_roles.sort(key=lambda r: r["_from_date_obj"], reverse=True)

        org = org_map.get(oid, {"O_ID": oid, "Name": oid, "Industry": ""})
        is_current_co = any(r["is_current"] for r in org_roles)
        earliest = min(r["_from_date_obj"] for r in org_roles)
        from_year = str(earliest.year)

        if is_current_co:
            to_label = "Present"
        else:
            latest_end = max(
                (parse_date(r["to_date"]) for r in org_roles if r["to_date"]),
                default=None,
            )
            to_label = str(latest_end.year) if latest_end else ""

        total_months_co = sum(r["_total_months"] for r in org_roles)
        co_yrs, co_mos = fmt_tenure(total_months_co)

        # Transitions for roles in this company
        transitions = []
        for r in org_roles:
            t = transition_map.get(r["id"], "")
            if t and t not in transitions:
                transitions.append(t)

        # Strip internal fields
        clean_roles = []
        for r in org_roles:
            cr = {k: v for k, v in r.items() if not k.startswith("_")}
            clean_roles.append(cr)

        companies.append({
            "org": {"O_ID": org.get("O_ID", oid), "Name": org.get("Name", oid), "Industry": org.get("Industry", "")},
            "is_current": is_current_co,
            "from_year": from_year,
            "to_label": to_label,
            "total_years": co_yrs,
            "total_months": co_mos,
            "transitions": transitions,
            "roles": clean_roles,
            "_earliest": earliest,
            "_is_current": is_current_co,
        })

    # Sort: current companies first, then by most recent start date
    companies.sort(key=lambda c: (not c["_is_current"], -c["_earliest"].toordinal()))
    for c in companies:
        del c["_earliest"]
        del c["_is_current"]

    # ── Stats ─────────────────────────────────────────────────────────────────
    all_role_flat = [r for c in companies for r in c["roles"]]

    def get_role_total_months(r):
        fd = parse_date(r["from_date"])
        td = parse_date(r["to_date"]) if r["to_date"] else None
        return months_between(fd, td)

    # Re-fetch analytics/consulting flags from original role rows
    role_flags = {}
    for r in role_rows:
        wid = r.get("W_ID", "")
        role_flags[wid] = {
            "analytics": str(r.get("Analytics", "")).strip().upper() == "Y",
            "consulting": str(r.get("Consulting", "")).strip().upper() == "Y",
        }

    total_months_all = sum(get_role_total_months(r) for r in all_role_flat)
    analytics_months = sum(
        get_role_total_months(r)
        for r in all_role_flat
        if role_flags.get(r["id"], {}).get("analytics")
    )
    consulting_months = sum(
        get_role_total_months(r)
        for r in all_role_flat
        if role_flags.get(r["id"], {}).get("consulting")
    )
    industries = len({c["org"]["Industry"] for c in companies if c["org"]["Industry"]})

    stats = {
        "total_exp": round(total_months_all / 12),
        "industries": industries,
        "analytics_years": round(analytics_months / 12),
        "consulting_years": round(consulting_months / 12),
    }

    # ── Radar axes ────────────────────────────────────────────────────────────
    # Category buckets from spec; "AI / ML" merges into "AI / ML / Coding"
    radar_category_map = {
        "Data & Analytics": 0,
        "Visualisation": 1,
        "AI / ML / Coding": 2,
        "AI / ML": 2,
        "CX": 3,
        "CRM": 4,
        "Team / Project Management": 5,
        "Team/Project Management": 5,
    }
    radar_scores = [0, 0, 0, 0, 0, 0]
    radar_skill_rows = [
        r for r in skill_rows if str(r.get("Radar Chart", "")).strip().upper() == "Y"
    ]
    radar_skill_ids = {r["S_ID"] for r in radar_skill_rows}

    for ts in task_skill_rows:
        sid = ts.get("S_ID", "")
        if sid in radar_skill_ids:
            sdata = skill_map.get(sid, {})
            cat = sdata.get("Skill Category", "")
            idx = radar_category_map.get(cat, -1)
            if idx >= 0:
                radar_scores[idx] += 1

    radar_max = max(radar_scores) if radar_scores else 1

    radar_axes = [
        {"label": ["Data &", "Analytics"], "score": radar_scores[0]},
        {"label": ["Visuali-", "sation"], "score": radar_scores[1]},
        {"label": ["AI/ML/", "Coding"], "score": radar_scores[2]},
        {"label": ["CX"], "score": radar_scores[3]},
        {"label": ["CRM"], "score": radar_scores[4]},
        {"label": ["Team/", "Project"], "score": radar_scores[5]},
    ]

    # ── Treemap top 5 (technical categories only) ─────────────────────────────
    EXCLUDE_CATS = {"Team / Project Management", "Team/Project Management"}
    skill_task_count = {}
    for ts in task_skill_rows:
        sid = ts.get("S_ID", "")
        sdata = skill_map.get(sid, {})
        cat = sdata.get("Skill Category", "")
        sname = sdata.get("Skill Name", "")
        if sname and cat not in EXCLUDE_CATS:
            skill_task_count[sname] = skill_task_count.get(sname, 0) + 1

    treemap_top5 = sorted(
        [{"name": k, "tasks": v} for k, v in skill_task_count.items()],
        key=lambda x: -x["tasks"],
    )[:5]

    # ── Education ─────────────────────────────────────────────────────────────
    edu_rows = sheets.get("Education", [])
    edu_major_rows = sheets.get("Education_Major", sheets.get("Education-Major", []))
    major_rows = sheets.get("Major", [])
    edu_loc_rows = sheets.get("Edu_Location", sheets.get("Edu-Location", []))

    major_map = {r["M_ID"]: r.get("Major Name", "") for r in major_rows}
    edu_major_map = {}
    for em in edu_major_rows:
        eid = em.get("E_ID", "")
        mid = em.get("M_ID", "")
        if eid and mid:
            edu_major_map.setdefault(eid, []).append(major_map.get(mid, ""))

    edu_loc_map = {}
    for el in edu_loc_rows:
        eid = el.get("E_ID", "")
        lid = el.get("L_ID", "")
        if eid and lid:
            edu_loc_map.setdefault(eid, []).append(lid)

    education = []
    for e in sorted(edu_rows, key=lambda x: int(x.get("Display Order", 99) or 99)):
        eid = e.get("E_ID", "")
        lids = edu_loc_map.get(eid, [])
        edu_city = ""
        edu_country = ""
        if lids:
            ldata = loc_map.get(lids[0], {})
            edu_city = ldata.get("City", "")
            edu_country = ldata.get("Country", "")
        edu_location = f"{edu_city}, {edu_country}" if edu_city and edu_country else edu_city or edu_country

        skill_sid = str(e.get("Skill (S_ID)", e.get("Skill", ""))).strip()
        skill_name = skill_map.get(skill_sid, {}).get("Skill Name", skill_sid) if skill_sid else ""

        from_year_raw = str(e.get("From_Date", "")).strip()
        to_year_raw = str(e.get("To_Date", "")).strip()
        from_year_val = from_year_raw[:4] if from_year_raw else ""
        to_year_val = to_year_raw[:4] if to_year_raw else ""

        education.append({
            "id": eid,
            "type": str(e.get("Type", "")).strip(),
            "institute": str(e.get("Institute", "")).strip(),
            "degree": str(e.get("Degree / Certificate", e.get("Degree", ""))).strip(),
            "majors": [m for m in edu_major_map.get(eid, []) if m],
            "city": edu_city,
            "country": edu_country,
            "location": edu_location,
            "from_year": from_year_val,
            "to_year": to_year_val,
            "skill": skill_name,
        })

    # ── Side Projects ─────────────────────────────────────────────────────────
    sp_rows = sheets.get("Side Projects", [])
    sp_skill_rows = sheets.get("Side_Projects___Skills", sheets.get("Side Projects - Skills", []))
    sp_skill_map = {}
    for ss in sp_skill_rows:
        spid = ss.get("SP_ID", "")
        sid = ss.get("S_ID", "")
        if spid and sid:
            sname = skill_map.get(sid, {}).get("Skill Name", "")
            if sname:
                sp_skill_map.setdefault(spid, []).append(sname)

    side_projects = []
    for sp in sorted(sp_rows, key=lambda x: int(x.get("Display Order Sequence", 99) or 99)):
        spid = str(sp.get("SP_ID", "")).strip()
        completed_raw = str(sp.get("Completed On", "")).strip()
        year_val = int(completed_raw[:4]) if completed_raw and completed_raw[:4].isdigit() else 0

        side_projects.append({
            "id": spid,
            "name": str(sp.get("Name", "")).strip(),
            "description": str(sp.get("Description", "")).strip(),
            "challenge": str(sp.get("Challenge", "")).strip(),
            "built_with": str(sp.get("Built with", "")).strip(),
            "output": str(sp.get("Output", "")).strip(),
            "year": year_val,
            "url": str(sp.get("URL", "")).strip(),
            "skills": sp_skill_map.get(spid, []),
        })

    return {
        "person": person,
        "stats": stats,
        "key_skills": key_skills_list,
        "all_skills": all_skills_list,
        "radar_axes": radar_axes,
        "radar_max": radar_max,
        "treemap_top5": treemap_top5,
        "companies": companies,
        "education": education,
        "side_projects": side_projects,
    }


# ── HTML generation ───────────────────────────────────────────────────────────

ICON_CHALLENGE = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>'
ICON_INPUT = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>'
ICON_OUTPUT = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/><line x1="2" y1="20" x2="22" y2="20"/></svg>'
ICON_OUTCOME = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>'
ICON_CHEVRON_DOWN = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>'
ICON_CHEVRON_UP = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg>'


CSS = r"""*{box-sizing:border-box;margin:0;padding:0;}
:root{
  --bg:#F2F3F6;--card:#fff;--obs:#181D25;--mist:#606E80;
  --cobalt:#3765F6;--cm:rgba(255,255,255,0.55);--cs:rgba(255,255,255,0.1);--cb:rgba(255,255,255,0.12);
  --mint:#70FC8E;--border:rgba(24,29,37,0.07);--bm:rgba(24,29,37,0.11);
  --mono:"Geist Mono",monospace;
}
.shell{background:linear-gradient(to right,var(--cobalt) 300px,var(--bg) 300px);display:grid;grid-template-columns:300px 1fr;min-height:100vh;font-family:var(--mono);}
/* background gradient extends Cobalt colour full page height — prevents sidebar bg stopping at viewport height */
.sb{background:var(--cobalt);padding:1.5rem 1.25rem;display:flex;flex-direction:column;gap:1.2rem;position:sticky;top:0;height:100dvh;overflow-y:auto;scrollbar-width:none;}
.sb::-webkit-scrollbar{display:none;}
.sb svg{flex-shrink:0;}
.sb-avatar{width:72px;height:72px;border-radius:50%;background:rgba(255,255,255,0.15);display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:20px;font-weight:700;color:#fff;margin-bottom:5px;}
.sb-name{font-family:var(--mono);font-size:20px;font-weight:700;color:#fff;margin-bottom:2px;}
.sb-role{font-size:16px;color:var(--cm);margin-bottom:8px;}
.sb-row{display:flex;align-items:flex-start;gap:6px;margin-bottom:5px;color:var(--cm);}
.sb-row svg{margin-top:1px;opacity:0.7;}
.sb-txt{font-size:12px;color:var(--cm);line-height:1.5;}
.reloc{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:10px;padding:2px 7px;border-radius:4px;background:rgba(255,255,255,0.12);color:rgba(255,255,255,0.75);margin-top:4px;}
.sb-div{border:none;border-top:0.5px solid var(--cb);}
.sb-lbl{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;color:var(--cm);margin-bottom:8px;}
.badges{display:flex;flex-direction:column;gap:5px;margin-bottom:8px;}
.cbadge{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;font-weight:700;padding:4px 9px;border-radius:4px;background:var(--mint);color:var(--obs);align-self:flex-start;}
.wr-lbl{font-size:11px;color:var(--cm);margin-bottom:4px;}
.wr-pills{display:flex;flex-wrap:wrap;gap:3px;}
.wr-pill{font-family:var(--mono);font-size:10px;padding:2px 6px;border-radius:4px;background:rgba(255,255,255,0.15);color:#fff;}
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;}
.stat-box{background:var(--cs);border-radius:10px;padding:8px 10px;}
.stat-val{font-family:var(--mono);font-size:20px;font-weight:700;color:#fff;line-height:1;}
.stat-lbl{font-size:10px;color:var(--cm);margin-top:3px;line-height:1.3;}
.sk-row{display:flex;flex-wrap:wrap;gap:4px;}
.sk-pill{font-size:11px;padding:3px 8px;border-radius:20px;font-family:var(--mono);background:rgba(255,255,255,0.18);color:#fff;}
.sk-note{font-size:10px;color:rgba(255,255,255,0.4);margin-top:6px;font-style:italic;line-height:1.4;}
.nav-item{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--cm);padding:6px 8px;border-radius:8px;cursor:pointer;margin-bottom:2px;user-select:none;}
.nav-item svg{opacity:0.6;}
.nav-item.active{background:rgba(255,255,255,0.18);color:#fff;}
.nav-item.active svg{opacity:1;color:var(--mint);}
.pdf-btn{display:flex;align-items:center;justify-content:center;gap:6px;font-family:var(--mono);font-size:11px;font-weight:600;color:var(--cobalt);background:#fff;border-radius:8px;padding:8px 12px;cursor:pointer;margin-top:4px;}
.main{padding:1.5rem;display:flex;flex-direction:column;gap:12px;background:var(--bg);}
.main-inner{max-width:640px;min-width:550px;width:100%;display:flex;flex-direction:column;gap:12px;}
.sec-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;}
.sec-ttl{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;color:var(--mist);}
.clp-btn{font-size:11px;color:var(--mist);display:flex;align-items:center;gap:3px;cursor:pointer;user-select:none;}
.sec-gap{margin-top:8px;}
.summary-card{background:var(--card);border:0.5px solid var(--bm);border-left:3px solid var(--cobalt);border-radius:0 14px 14px 0;padding:1.1rem 1.25rem;margin-bottom:10px;}
.summary-lede{font-size:14px;font-weight:600;color:var(--obs);line-height:1.6;margin-bottom:8px;display:block;}
.summary-rest{font-size:12px;color:var(--mist);line-height:1.8;display:block;}
.skills-card{background:var(--card);border:0.5px solid var(--bm);border-radius:14px;padding:12px;display:flex;gap:12px;height:200px;margin-bottom:10px;}
.card-divider{width:0.5px;background:rgba(24,29,37,0.08);flex-shrink:0;align-self:stretch;}
.radar-wrap{flex:2;min-width:0;display:flex;flex-direction:column;}
.radar-inner{background:#EEF2FF;border-radius:0;flex:1;position:relative;overflow:hidden;}
.radar-inner canvas{position:absolute;top:0;left:0;width:100%;height:100%;}
.treemap-wrap{flex:3;min-width:0;display:flex;flex-direction:column;}
.treemap-inner{background:#fff;flex:1;display:flex;flex-direction:column;gap:3px;overflow:hidden;}
.tm-row{display:flex;gap:3px;flex:1;}
.tm-cell{border-radius:0;padding:10px 12px;overflow:hidden;display:flex;flex-direction:column;min-width:0;background:#EEF2FF;transition:filter 0.15s;}
.tm-cell:hover{filter:brightness(0.96);}
.tm-top{display:flex;flex-direction:column;flex:1;}
.tm-name{font-size:11px;font-weight:700;color:var(--cobalt);line-height:1.3;margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.tm-sub{font-size:9px;color:rgba(55,101,246,0.55);}
.tm-bottom{display:flex;align-items:baseline;gap:4px;margin-top:auto;padding-top:6px;}
.tm-count{font-size:24px;font-weight:700;color:var(--cobalt);line-height:1;opacity:0.85;}
.tm-lbl{font-size:9px;color:rgba(55,101,246,0.55);}
.card{background:var(--card);border:0.5px solid var(--bm);border-radius:14px;padding:1.1rem 1.1rem 0.5rem;margin-bottom:10px;}
.card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:2px;}
.card-title{font-family:var(--mono);font-size:14px;font-weight:700;color:var(--obs);}
.card-company{font-family:var(--mono);font-size:13px;font-weight:700;color:var(--cobalt);margin-top:2px;}
.card-meta{font-size:12px;color:var(--mist);margin-top:1px;margin-bottom:10px;}
.badge-current{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:20px;background:var(--obs);color:var(--mint);font-weight:700;white-space:nowrap;}
.tenure-block{text-align:right;flex-shrink:0;display:flex;flex-direction:column;align-items:flex-end;gap:4px;}
.tenure-num{font-family:var(--mono);font-size:26px;font-weight:700;color:var(--obs);line-height:1;}
.tenure-num span{font-size:14px;color:var(--mist);font-weight:500;}
.tenure-dates{font-family:var(--mono);font-size:10px;color:var(--mist);}
.chip-row{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:10px;}
.chip{font-size:11px;padding:2px 8px;border-radius:4px;font-family:var(--mono);background:#EEF2FF;color:#3765F6;}
.tasks-lbl{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;color:var(--mist);margin-bottom:6px;margin-top:2px;}
.task-row{border-top:0.5px solid var(--border);padding:9px 0;}
.task-line{display:flex;align-items:flex-start;gap:7px;}
.tdot{width:4px;height:4px;border-radius:50%;background:var(--cobalt);opacity:0.35;margin-top:8px;flex-shrink:0;}
.ttxt{font-size:12px;color:var(--mist);line-height:1.5;flex:1;}
.vd-btn{flex-shrink:0;font-size:11px;font-family:var(--mono);color:var(--cobalt);border:0.5px solid var(--cobalt);border-radius:4px;padding:2px 8px;background:transparent;cursor:pointer;white-space:nowrap;user-select:none;}
.vd-btn.open{background:#EEF2FF;}
.dp{overflow:hidden;max-height:0;opacity:0;transition:max-height 0.35s ease,opacity 0.25s;}
.dp-inner{padding:8px 0 6px 11px;}
.task-chip-row{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px;}
.task-chip{font-size:10px;padding:2px 7px;border-radius:4px;font-family:var(--mono);background:#EEF2FF;color:#3765F6;}
.task-desc{font-size:12px;color:var(--obs);line-height:1.6;margin-bottom:8px;padding:8px 10px;background:var(--bg);border-radius:8px;border-left:2px solid var(--cobalt);}
.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;}
.dpc{background:var(--bg);border-radius:10px;padding:9px 10px;}
.dpl{font-family:var(--mono);font-size:9px;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;color:var(--mist);margin-bottom:4px;display:flex;align-items:center;gap:4px;}
.dpl svg{flex-shrink:0;}
.dpv{font-size:12px;color:var(--obs);line-height:1.45;}
.dpc.oc{background:var(--mint);}
.dpc.oc .dpl{color:var(--obs);opacity:0.6;}
.dpc.oc .dpv{color:var(--obs);font-family:var(--mono);font-size:12px;font-weight:600;}
.extra-tasks{overflow:hidden;max-height:0;opacity:0;transition:max-height 0.4s ease,opacity 0.3s;}
.show-more-row{border-top:0.5px solid var(--border);padding:9px 0 6px;}
.show-more-btn{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-family:var(--mono);color:var(--mist);border:0.5px solid #606E80;border-radius:4px;padding:3px 10px;cursor:pointer;user-select:none;background:transparent;}
.show-more-btn:hover{background:#E8EAEC;}
.company-card{background:var(--card);border:0.5px solid var(--bm);border-radius:14px;padding:1rem 1.1rem;margin-bottom:10px;}
.company-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:12px;padding-bottom:10px;border-bottom:0.5px solid var(--border);}
.company-name{font-family:var(--mono);font-size:14px;font-weight:700;color:var(--cobalt);margin-bottom:2px;}
.company-meta{font-size:12px;color:var(--mist);}
.role-block{border-left:2px solid var(--border);margin-left:4px;padding-left:14px;margin-bottom:14px;}
.role-block:last-child{margin-bottom:0;}
.role-block.current{border-left-color:var(--mint);}
.role-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:4px;}
.role-title{font-family:var(--mono);font-size:13px;font-weight:700;color:var(--obs);}
.role-tenure-num{font-family:var(--mono);font-size:18px;font-weight:700;color:var(--obs);line-height:1;}
.role-tenure-num span{font-size:11px;color:var(--mist);font-weight:500;}
.role-dates{font-family:var(--mono);font-size:10px;color:var(--mist);text-align:right;margin-top:2px;}
.promo-row{display:flex;align-items:center;gap:6px;margin:10px 0 10px -18px;padding-left:4px;}
.promo-line{flex:1;height:0.5px;background:var(--border);}
.promo-badge{font-family:var(--mono);font-size:9px;font-weight:600;color:var(--mist);background:var(--bg);padding:2px 8px;border-radius:20px;border:0.5px solid var(--border);white-space:nowrap;}
.proj-card{background:var(--card);border:0.5px solid var(--bm);border-radius:14px;padding:1.1rem;margin-bottom:10px;}
.proj-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:4px;}
.proj-title{font-family:var(--mono);font-size:14px;font-weight:700;color:var(--obs);}
.proj-meta{font-size:12px;color:var(--mist);margin-top:2px;margin-bottom:6px;}
.proj-desc{font-size:12px;color:var(--mist);margin-bottom:10px;line-height:1.5;}
.proj-btns{display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex-shrink:0;}
.proj-link{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:10px;color:var(--cobalt);border:0.5px solid var(--cobalt);border-radius:4px;padding:3px 8px;text-decoration:none;white-space:nowrap;cursor:pointer;}
.proj-dp{overflow:hidden;max-height:0;opacity:0;transition:max-height 0.35s ease,opacity 0.25s;}
.proj-dp-inner{padding:8px 0 4px 0;}
.proj-detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;}
.edu-card{background:var(--card);border:0.5px solid var(--bm);border-radius:14px;padding:1rem 1.1rem;margin-bottom:10px;}
.edu-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:10px;}
.edu-left{flex:1;}
.edu-deg{font-family:var(--mono);font-size:14px;font-weight:700;color:var(--obs);margin-bottom:3px;}
.edu-inst{font-size:12px;color:var(--mist);margin-bottom:6px;}
.edu-meta{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:8px;}
.edu-lvl{font-family:var(--mono);font-size:10px;font-weight:600;padding:2px 8px;border-radius:4px;background:#E8EAEC;color:#606E80;}
.edu-loc{display:flex;align-items:center;gap:3px;font-size:11px;color:var(--mist);}
.mjr-row{display:flex;align-items:center;flex-wrap:wrap;gap:5px;}
.mjr-lbl{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:0.07em;text-transform:uppercase;color:var(--mist);flex-shrink:0;}
.mjr-chip{font-size:11px;padding:2px 8px;border-radius:4px;font-family:var(--mono);background:#EEEDFE;color:#3C3489;}
.edu-yr{font-family:var(--mono);font-size:22px;font-weight:700;color:var(--obs);line-height:1;text-align:right;}
.edu-yr-range{font-family:var(--mono);font-size:10px;color:var(--mist);text-align:right;margin-top:2px;}
.edu-div{border:none;border-top:0.5px solid var(--border);margin:8px 0;}
.edu-pills{display:flex;flex-wrap:wrap;gap:4px;}
.edu-pill{font-size:11px;padding:2px 8px;border-radius:4px;font-family:var(--mono);background:#EEF2FF;color:#3765F6;}
.cert-badge{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:20px;background:var(--obs);color:var(--mint);font-weight:700;white-space:nowrap;}
.cv-footer{padding-bottom:1.5rem;text-align:right;margin-top:8px;}
.cv-footer span{font-family:var(--mono);font-size:10px;color:var(--mist);}
.pdf-page{display:none;}
@media print{.shell{display:none !important;}.pdf-page{display:block !important;}*{-webkit-print-color-adjust:exact;print-color-adjust:exact;}}
.pdf-page{background:#fff;font-family:var(--mono);color:var(--obs);max-width:680px;margin:0 auto;padding:2rem 2.25rem;}
.p-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px;gap:16px;}
.p-left{flex:1;}
.p-name{font-family:var(--mono);font-size:22px;font-weight:700;color:var(--obs);margin-bottom:3px;}
.p-role{font-family:var(--mono);font-size:11px;font-weight:600;color:var(--cobalt);letter-spacing:0.04em;margin-bottom:9px;}
.p-ci{font-size:11px;color:var(--mist);display:inline-flex;align-items:center;gap:3px;}
.p-ci::after{content:"·";margin:0 7px;color:rgba(24,29,37,0.2);}
.p-ci:last-child::after{content:"";}
.p-reloc{font-size:11px;color:var(--mist);margin-top:3px;margin-bottom:7px;}
.p-cit-row{display:flex;align-items:center;gap:6px;margin-bottom:4px;flex-wrap:wrap;}
.p-cit-lbl{font-size:10px;color:var(--mist);}
.p-cit-badge{font-family:var(--mono);font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;background:var(--obs);color:#fff;}
.p-wr-row{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-top:4px;}
.p-wr-lbl{font-size:10px;color:var(--mist);}
.p-wr-pill{font-family:var(--mono);font-size:9px;padding:2px 7px;border-radius:3px;border:0.5px solid rgba(24,29,37,0.1);color:var(--mist);}
.p-qr{display:flex;flex-direction:column;align-items:center;gap:5px;flex-shrink:0;}
.p-qr img{width:72px;height:72px;border:0.5px solid rgba(24,29,37,0.1);border-radius:4px;}
.p-qr-lbl{font-family:var(--mono);font-size:9px;color:var(--mist);text-align:center;line-height:1.3;max-width:72px;}
.p-hdiv{border:none;border-top:2px solid var(--cobalt);margin-bottom:14px;}
.p-stats{display:grid;grid-template-columns:repeat(4,1fr);margin-bottom:14px;gap:4px;}
.p-sc{background:var(--cobalt);padding:10px 12px;border-radius:6px;}
.p-sn{font-family:var(--mono);font-size:20px;font-weight:700;color:#fff;line-height:1;}
.p-sl{font-size:10px;color:rgba(255,255,255,0.7);margin-top:3px;}
.p-summary{margin-bottom:14px;}
.p-summary-lede{font-size:12px;font-weight:600;color:var(--obs);line-height:1.6;display:block;margin-bottom:5px;}
.p-summary-rest{font-size:11px;color:var(--mist);line-height:1.7;display:block;}
.p-sec{margin-bottom:16px;}
.p-st{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:var(--cobalt);margin-bottom:7px;padding-bottom:3px;border-bottom:0.5px solid rgba(55,101,246,0.35);}
.p-sk-wrap{display:flex;flex-wrap:wrap;gap:4px;}
.p-sk{font-family:var(--mono);font-size:9px;padding:2px 7px;border-radius:3px;border:0.5px solid rgba(24,29,37,0.1);color:var(--obs);background:#fff;}
.p-ei{margin-bottom:14px;padding-bottom:14px;border-bottom:0.5px solid rgba(24,29,37,0.1);}
.p-ei:last-child{border-bottom:none;margin-bottom:0;padding-bottom:0;}
.p-eh{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:1px;}
.p-etitle{font-family:var(--mono);font-size:12px;font-weight:700;color:var(--obs);}
.p-etenure{font-family:var(--mono);font-size:11px;font-weight:700;color:var(--obs);}
.p-ecompany{font-family:var(--mono);font-size:12px;font-weight:700;color:var(--cobalt);margin-bottom:2px;}
.p-emeta{font-size:10px;color:var(--mist);margin-bottom:7px;}
.p-tlist{list-style:none;padding:0;}
.p-tlist li{padding-left:12px;position:relative;margin-bottom:5px;}
.p-tlist li:last-child{margin-bottom:0;}
.p-tlist li::before{content:"–";position:absolute;left:0;color:var(--cobalt);font-weight:700;font-size:10px;top:1px;}
.p-tname{font-size:11px;font-weight:600;color:var(--obs);line-height:1.4;}
.p-tchip-row{display:flex;flex-wrap:wrap;gap:3px;margin-bottom:5px;}
.p-tchip{font-family:var(--mono);font-size:9px;padding:1px 6px;border-radius:3px;background:#EEF2FF;color:#3765F6;}
.p-ttwo{display:grid;grid-template-columns:2fr 1fr;gap:5px;}
.p-tcell{background:var(--bg);border-radius:5px;padding:6px 8px;}
.p-tclbl{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--mist);margin-bottom:3px;}
.p-tcval{font-size:10px;color:var(--obs);line-height:1.45;}
.p-tcell.p-oc{background:var(--mint);}
.p-tcell.p-oc .p-tclbl{color:var(--obs);opacity:0.55;}
.p-tcell.p-oc .p-tcval{color:var(--obs);font-family:var(--mono);font-size:10px;font-weight:600;}
.p-note{margin-top:10px;font-size:9px;color:var(--mist);font-style:italic;padding:7px 12px;background:#F2F3F6;border-radius:4px;border-left:2px solid var(--cobalt);}
.p-teaser{display:flex;align-items:center;gap:20px;background:#F2F3F6;border-radius:10px;padding:14px 16px;}
.p-teaser-left{flex:1;}
.p-teaser-title{font-family:var(--mono);font-size:11px;font-weight:700;color:var(--obs);margin-bottom:5px;}
.p-teaser-body{font-size:11px;color:var(--mist);line-height:1.6;}
.p-teaser-url{font-family:var(--mono);font-size:9px;color:var(--cobalt);margin-top:6px;display:block;}
.p-qr2{display:flex;flex-direction:column;align-items:center;gap:5px;flex-shrink:0;}
.p-qr2 img{width:88px;height:88px;border:0.5px solid rgba(24,29,37,0.1);border-radius:4px;}
.p-qr2-lbl{font-family:var(--mono);font-size:9px;color:var(--mist);text-align:center;line-height:1.3;max-width:88px;}
.p-edui{margin-bottom:11px;}
.p-eduh{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:2px;}
.p-edudeg{font-family:var(--mono);font-size:12px;font-weight:700;color:var(--obs);}
.p-eduyr{font-family:var(--mono);font-size:11px;font-weight:700;color:var(--obs);}
.p-eduinst{font-size:10px;color:var(--mist);margin-bottom:3px;}
.p-edulvl{font-family:var(--mono);font-size:9px;font-weight:600;padding:1px 6px;border-radius:3px;background:#EEF2FF;color:var(--cobalt);display:inline-block;margin-bottom:3px;}
.p-mjrow{display:flex;align-items:center;gap:5px;flex-wrap:wrap;}
.p-mjlbl{font-family:var(--mono);font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:0.06em;color:var(--mist);}
.p-mjchip{font-family:var(--mono);font-size:9px;padding:1px 6px;border-radius:3px;background:#EEEDFE;color:#3C3489;}
.p-certbadge{font-family:var(--mono);font-size:9px;font-weight:700;padding:3px 8px;border-radius:20px;background:var(--obs);color:var(--mint);white-space:nowrap;}
.p-cskills{display:flex;flex-wrap:wrap;gap:3px;margin-top:3px;}
.p-csk{font-family:var(--mono);font-size:9px;padding:1px 6px;border-radius:3px;background:#F2F3F6;color:var(--mist);}
.p-foot{margin-top:20px;padding-top:10px;border-top:0.5px solid rgba(24,29,37,0.1);display:flex;justify-content:space-between;}
.p-fn{font-family:var(--mono);font-size:9px;color:var(--mist);}
.p-fd{font-size:9px;color:rgba(24,29,37,0.25);}
.p-edui{break-inside:avoid;page-break-inside:avoid;}
.p-tlist li{break-inside:avoid;page-break-inside:avoid;}
.p-tname{break-after:avoid;page-break-after:avoid;}
.p-st{break-after:avoid;page-break-after:avoid;}
.p-ecompany{break-after:avoid;page-break-after:avoid;}
.sb-motto{font-size:11px;color:var(--cm);line-height:1.6;font-style:italic;margin-top:12px;margin-bottom:25px;opacity:0.85;}
.sb-motto-quote{color:var(--mint);font-style:normal;font-weight:700;}"""


def build_task_html(task):
    chips = "".join(
        f'<span class="task-chip">{esc(s["name"])}</span>'
        for s in task.get("skills", [])
    )
    chip_row = f'<div class="task-chip-row">{chips}</div>' if chips else ""

    desc_html = (
        f'<div class="task-desc">{esc(task["description"])}</div>'
        if task.get("description") else ""
    )

    def dpc(label, icon, value, extra_class=""):
        if not value:
            return ""
        return (
            f'<div class="dpc{" " + extra_class if extra_class else ""}">'
            f'<div class="dpl">{icon}{label}</div>'
            f'<div class="dpv">{esc(value)}</div>'
            f'</div>'
        )

    grid = (
        dpc("Challenge", ICON_CHALLENGE, task.get("challenge", "")) +
        dpc("Input", ICON_INPUT, task.get("input", "")) +
        dpc("Output", ICON_OUTPUT, task.get("output", "")) +
        dpc("Outcome", ICON_OUTCOME, task.get("outcome", ""), "oc")
    )
    grid_html = f'<div class="detail-grid">{grid}</div>' if grid else ""

    return (
        f'<div class="task-line">'
        f'<span class="tdot"></span>'
        f'<span class="ttxt">{esc(task["name"])}</span>'
        f'<span class="vd-btn" onclick="toggleDetail(this)">View details ↓</span>'
        f'</div>'
        f'<div class="dp">'
        f'<div class="dp-inner">'
        f'{chip_row}{desc_html}{grid_html}'
        f'</div>'
        f'</div>'
    )


def build_role_tasks_html(role, card_id_prefix):
    tasks = role.get("tasks", [])
    if not tasks:
        return ""

    VISIBLE = 3
    visible_tasks = tasks[:VISIBLE]
    hidden_tasks = tasks[VISIBLE:]

    visible_html = "".join(
        f'<div class="task-row">{build_task_html(t)}</div>'
        for t in visible_tasks
    )

    if hidden_tasks:
        extra_id = f"{card_id_prefix}-extra"
        n = len(hidden_tasks)
        hidden_html = "".join(
            f'<div class="task-row">{build_task_html(t)}</div>'
            for t in hidden_tasks
        )
        show_more = (
            f'<div class="extra-tasks" id="{extra_id}">{hidden_html}</div>'
            f'<div class="show-more-row">'
            f'<span class="show-more-btn" onclick="toggleShowMore(\'{extra_id}\',this,{n})">'
            f'{ICON_CHEVRON_DOWN} Show {n} more task{"s" if n > 1 else ""}'
            f'</span></div>'
        )
    else:
        show_more = ""

    return visible_html + show_more


def build_single_role_card(company):
    role = company["roles"][0]
    org = company["org"]
    yrs = role["tenure_years"]
    mos = role["tenure_months"]
    from_year = company["from_year"]
    to_label = company["to_label"]
    is_current = company["is_current"]

    locs_str = " · ".join(role.get("locations", []))
    meta = f'{esc(org.get("Industry",""))}&nbsp;·&nbsp;{esc(locs_str)}' if locs_str else esc(org.get("Industry",""))

    current_badge = '<span class="badge-current">CURRENT</span>' if is_current else ""

    chips = "".join(
        f'<span class="chip">{esc(s["name"])}</span>'
        for s in role.get("all_skills", [])
    )
    chip_row = f'<div class="chip-row">{chips}</div>' if chips else ""

    card_id = f'role-{role["id"]}'
    tasks_html = build_role_tasks_html(role, card_id)

    return (
        f'<div class="card">'
        f'<div class="card-head">'
        f'<div>'
        f'<div class="card-title">{esc(role["role"])}</div>'
        f'<div class="card-company">{esc(org["Name"])}</div>'
        f'<div class="card-meta">{meta}</div>'
        f'</div>'
        f'<div class="tenure-block">'
        f'{current_badge}'
        f'<div class="tenure-num">{tenure_html(yrs, mos)}</div>'
        f'<div class="tenure-dates">{from_year} – {to_label}</div>'
        f'</div>'
        f'</div>'
        f'{chip_row}'
        f'<div class="tasks-lbl">Key Tasks</div>'
        f'{tasks_html}'
        f'</div>'
    )


def build_multi_role_card(company):
    org = company["org"]
    from_year = company["from_year"]
    to_label = company["to_label"]
    co_yrs = company["total_years"]
    co_mos = company["total_months"]
    is_current = company["is_current"]

    current_badge = '<span class="badge-current">CURRENT</span>' if is_current else ""

    roles_html = ""
    for i, role in enumerate(company["roles"]):
        r_yrs = role["tenure_years"]
        r_mos = role["tenure_months"]
        r_from = role["from_date"][:4] if role["from_date"] else ""
        r_to = role["to_date"][:4] if role["to_date"] else "Present"
        r_current = role["is_current"]

        current_cls = " current" if r_current else ""

        # Promotion badge between roles (after the first block, before the next)
        promo_html = ""
        if i > 0:
            t = company["transitions"][i - 1] if i - 1 < len(company["transitions"]) else ""
            if t:
                promo_html = (
                    f'<div class="promo-row">'
                    f'<div class="promo-line"></div>'
                    f'<span class="promo-badge">{esc(t)}</span>'
                    f'<div class="promo-line"></div>'
                    f'</div>'
                )

        chips = "".join(
            f'<span class="chip">{esc(s["name"])}</span>'
            for s in role.get("all_skills", [])
        )
        chip_row = f'<div class="chip-row">{chips}</div>' if chips else ""

        card_id = f'role-{role["id"]}'
        tasks_html = build_role_tasks_html(role, card_id)

        roles_html += promo_html + (
            f'<div class="role-block{current_cls}">'
            f'<div class="role-head">'
            f'<div class="role-title">{esc(role["role"])}</div>'
            f'<div style="text-align:right;flex-shrink:0;">'
            f'<div class="role-tenure-num">{tenure_html(r_yrs, r_mos).replace("tenure-num", "role-tenure-num")}</div>'
            f'<div class="role-dates">{r_from} – {r_to}</div>'
            f'</div>'
            f'</div>'
            f'{chip_row}'
            f'<div class="tasks-lbl">Key Tasks</div>'
            f'{tasks_html}'
            f'</div>'
        )

    locs_str = " · ".join(
        loc
        for role in company["roles"]
        for loc in role.get("locations", [])
        if loc not in [
            loc2
            for j, r2 in enumerate(company["roles"])
            for loc2 in (r2.get("locations", []) if j < company["roles"].index(role) else [])
        ]
    )

    return (
        f'<div class="company-card">'
        f'<div class="company-head">'
        f'<div>'
        f'<div class="company-name">{esc(org["Name"])}</div>'
        f'<div class="company-meta">{esc(org.get("Industry",""))}&nbsp;·&nbsp;{from_year} – {to_label}</div>'
        f'</div>'
        f'<div class="tenure-block">'
        f'{current_badge}'
        f'<div class="tenure-num">{tenure_html(co_yrs, co_mos)}</div>'
        f'</div>'
        f'</div>'
        f'{roles_html}'
        f'</div>'
    )


def build_experience_html(d):
    cards = []
    for company in d["companies"]:
        if len(company["roles"]) == 1:
            cards.append(build_single_role_card(company))
        else:
            cards.append(build_multi_role_card(company))
    return "".join(cards)


def build_projects_html(d):
    cards = []
    for sp in d["side_projects"]:
        spid = sp["id"]
        year_str = str(sp["year"]) if sp.get("year") else ""
        chips = "".join(f'<span class="chip">{esc(s)}</span>' for s in sp.get("skills", []))
        chip_row = f'<div class="chip-row">{chips}</div>' if chips else ""

        link_btn = ""
        if sp.get("url"):
            link_btn = (
                f'<a class="proj-link" href="{esc(sp["url"])}" target="_blank">'
                f'<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>'
                f'View project</a>'
            )

        detail_btn = (
            f'<span class="proj-link" style="cursor:pointer;" onclick="toggleProjDetail(this)">'
            f'{ICON_CHEVRON_DOWN} View details'
            f'</span>'
        )

        def proj_dpc(label, value, extra_cls=""):
            if not value:
                return ""
            return (
                f'<div class="dpc{" "+extra_cls if extra_cls else ""}">'
                f'<div class="dpl">{label}</div>'
                f'<div class="dpv">{esc(value)}</div>'
                f'</div>'
            )

        detail_grid = (
            proj_dpc("Challenge", sp.get("challenge", "")) +
            proj_dpc("Built with", sp.get("built_with", "")) +
            proj_dpc("Output", sp.get("output", ""), "oc")
        )

        cards.append(
            f'<div class="proj-card">'
            f'<div class="proj-head">'
            f'<div>'
            f'<div class="proj-title">{esc(sp["name"])}</div>'
            f'<div class="proj-meta">{year_str}</div>'
            f'</div>'
            f'<div class="proj-btns">{link_btn}{detail_btn}</div>'
            f'</div>'
            f'<div class="proj-desc">{esc(sp.get("description",""))}</div>'
            f'{chip_row}'
            f'<div class="proj-dp">'
            f'<div class="proj-dp-inner">'
            f'<div class="proj-detail-grid">{detail_grid}</div>'
            f'</div>'
            f'</div>'
            f'</div>'
        )
    return "".join(cards)


def build_education_html(d):
    cards = []
    for e in d["education"]:
        e_type = e.get("type", "")
        is_cert = e_type.lower() in ("certificate", "certification", "cert")

        loc_parts = [p for p in [e.get("city", ""), e.get("country", "")] if p]
        loc_str = ", ".join(loc_parts)
        loc_icon = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>'

        yr_display = e.get("to_year", "") or e.get("from_year", "")
        yr_range = ""
        if e.get("from_year") and e.get("to_year") and e["from_year"] != e["to_year"]:
            yr_range = f'{e["from_year"]} – {e["to_year"]}'

        if is_cert:
            cert_badge = f'<span class="cert-badge">Certified {yr_display}</span>' if yr_display else ""
            skill_pill = ""
            if e.get("skill"):
                skill_pill = (
                    f'<div class="edu-div"></div>'
                    f'<div class="edu-pills">'
                    f'<span class="edu-pill">{esc(e["skill"])}</span>'
                    f'</div>'
                )
            cards.append(
                f'<div class="edu-card">'
                f'<div class="edu-head">'
                f'<div class="edu-left">'
                f'<div class="edu-deg">{esc(e["degree"])}</div>'
                f'<div class="edu-inst">{esc(e["institute"])}</div>'
                f'<div class="edu-meta">'
                f'<span class="edu-lvl">{esc(e_type)}</span>'
                f'</div>'
                f'</div>'
                f'<div>{cert_badge}</div>'
                f'</div>'
                f'{skill_pill}'
                f'</div>'
            )
        else:
            majors_html = ""
            if e.get("majors"):
                chips = "".join(f'<span class="mjr-chip">{esc(m)}</span>' for m in e["majors"])
                majors_html = f'<div class="mjr-row"><span class="mjr-lbl">Major(s)</span>{chips}</div>'

            yr_range_html = f'<div class="edu-yr-range">{yr_range}</div>' if yr_range else ""

            loc_div = f'<div class="edu-loc">{loc_icon} {esc(loc_str)}</div>' if loc_str else ""
            cards.append(
                f'<div class="edu-card">'
                f'<div class="edu-head">'
                f'<div class="edu-left">'
                f'<div class="edu-deg">{esc(e["degree"])}</div>'
                f'<div class="edu-inst">{esc(e["institute"])}</div>'
                f'<div class="edu-meta">'
                f'<span class="edu-lvl">{esc(e_type)}</span>'
                f'{loc_div}'
                f'</div>'
                f'{majors_html}'
                f'</div>'
                f'<div>'
                f'<div class="edu-yr">{yr_display}</div>'
                f'{yr_range_html}'
                f'</div>'
                f'</div>'
                f'</div>'
            )
    return "".join(cards)


def build_pdf_html(d):
    p = d["person"]
    s = d["stats"]

    # Header
    location_str = esc(p.get("location", ""))
    phone_str = esc(p.get("phone", ""))
    email_str = esc(p.get("email", ""))
    reloc_html = f'<div class="p-reloc">Open to relocation &rarr; {esc(p.get("relocation",""))}</div>' if p.get("relocation") and p.get("country") != "Australia" else ""

    phone2_str = esc(p.get("phone2", ""))
    ci_items = [location_str, phone_str]
    if phone2_str:
        ci_items.append(phone2_str)
    ci_items.append(email_str)
    ci_html = "".join(f'<span class="p-ci">{v}</span>' for v in ci_items if v)

    skills_html = "".join(f'<span class="p-sk">{esc(sk)}</span>' for sk in d.get("all_skills", []))

    # Experience entries — one p-ei per role across all companies
    exp_html = ""
    for company in d["companies"]:
        for role in company["roles"]:
            yrs = role["tenure_years"]
            mos = role["tenure_months"]
            tenure_str = ""
            if yrs:
                tenure_str += f"{yrs}yr "
            if mos:
                tenure_str += f"{mos}mo"
            tenure_str = tenure_str.strip()

            from_yr = role["from_date"][:4] if role["from_date"] else ""
            to_yr = role["to_date"][:4] if role["to_date"] else "Present"
            locs_str = " · ".join(role.get("locations", []))
            meta_parts = [esc(company["org"].get("Industry","")), esc(locs_str), f"{from_yr} – {to_yr}"]
            meta_str = "&nbsp;·&nbsp;".join(p2 for p2 in meta_parts if p2)

            task_items = "".join(
                f'<li><div class="p-tname">{esc(t["name"])}</div></li>'
                for t in role.get("tasks", [])
            )

            exp_html += (
                f'<div class="p-ei">'
                f'<div class="p-eh">'
                f'<div class="p-etitle">{esc(role["role"])}</div>'
                f'<div class="p-etenure">{tenure_str}</div>'
                f'</div>'
                f'<div class="p-ecompany">{esc(company["org"]["Name"])}</div>'
                f'<div class="p-emeta">{meta_str}</div>'
                f'<ul class="p-tlist">{task_items}</ul>'
                f'</div>'
            )

    # Side projects teaser
    sp_names = [sp["name"] for sp in d.get("side_projects", [])]
    if len(sp_names) > 3:
        sp_preview = ", ".join(sp_names[:3]) + f", and {len(sp_names)-3} more"
    else:
        sp_preview = ", ".join(sp_names)

    teaser_html = (
        f'<div class="p-teaser">'
        f'<div class="p-teaser-left">'
        f'<div class="p-teaser-title">There&rsquo;s more beyond this page.</div>'
        f'<div class="p-teaser-body">I work on a number of side projects that sit outside a traditional CV format — {esc(sp_preview)}. Scan the QR code to explore them in full on the interactive version of this CV.</div>'
        f'<span class="p-teaser-url">{QR_URL}</span>'
        f'</div>'
        f'<div class="p-qr2">'
        f'<img id="pdf-qr2-img" src="" alt="QR">'
        f'<div class="p-qr2-lbl">Scan to explore side projects</div>'
        f'</div>'
        f'</div>'
    )

    # Education
    edu_html = ""
    for e in d["education"]:
        e_type = e.get("type", "")
        is_cert = e_type.lower() in ("certificate", "certification", "cert")
        yr_display = e.get("to_year", "") or e.get("from_year", "")

        if is_cert:
            cert_badge = f'<span class="p-certbadge">Certified {yr_display}</span>' if yr_display else ""
            skill_html = ""
            if e.get("skill"):
                skill_html = f'<div class="p-cskills"><span class="p-csk">{esc(e["skill"])}</span></div>'
            edu_html += (
                f'<div class="p-edui">'
                f'<div class="p-eduh">'
                f'<div class="p-edudeg">{esc(e["degree"])}</div>'
                f'{cert_badge}'
                f'</div>'
                f'<div class="p-eduinst">{esc(e["institute"])}</div>'
                f'{skill_html}'
                f'</div>'
            )
        else:
            loc_str = f'{e["city"]}, {e["country"]}' if e.get("city") and e.get("country") else e.get("city","") or e.get("country","")
            majors_html = ""
            if e.get("majors"):
                chips = "".join(f'<span class="p-mjchip">{esc(m)}</span>' for m in e["majors"])
                majors_html = f'<div class="p-mjrow"><span class="p-mjlbl">Major(s)</span>{chips}</div>'
            edu_html += (
                f'<div class="p-edui">'
                f'<div class="p-eduh">'
                f'<div class="p-edudeg">{esc(e["degree"])}</div>'
                f'<div class="p-eduyr">{yr_display}</div>'
                f'</div>'
                f'<div class="p-eduinst">{esc(e["institute"])} · {esc(loc_str)}</div>'
                f'<div><span class="p-edulvl">{esc(e_type)}</span></div>'
                f'{majors_html}'
                f'</div>'
            )

    today_str = TODAY.strftime("%d %B %Y").lstrip("0")

    return (
        f'<div class="pdf-page" id="pdf-page">'
        f'<div class="p-top">'
        f'<div class="p-left">'
        f'<div class="p-name">{esc(p["name"])}</div>'
        f'<div class="p-role">{esc(p["role"])}</div>'
        f'<div style="margin-bottom:4px;">{ci_html}</div>'
        f'{reloc_html}'
        f'<div class="p-cit-row"><span class="p-cit-lbl">Citizenship</span><span class="p-cit-badge">NZ Citizen</span><span class="p-cit-badge">Thai Citizen</span></div>'
        f'<div class="p-wr-row"><span class="p-wr-lbl">Eligible to work in</span><span class="p-wr-pill">Australia</span><span class="p-wr-pill">New Zealand</span><span class="p-wr-pill">Thailand</span><span class="p-wr-pill">SE Asia</span></div>'
        f'</div>'
        f'<div class="p-qr">'
        f'<img id="pdf-qr-img" src="" alt="QR">'
        f'<div class="p-qr-lbl">View interactive CV</div>'
        f'</div>'
        f'</div>'
        f'<hr class="p-hdiv">'
        f'<div class="p-stats">'
        f'<div class="p-sc"><div class="p-sn">{s["total_exp"]}</div><div class="p-sl">Yrs experience</div></div>'
        f'<div class="p-sc"><div class="p-sn">{s["industries"]}</div><div class="p-sl">Industries</div></div>'
        f'<div class="p-sc"><div class="p-sn">{s["analytics_years"]}</div><div class="p-sl">Yrs analytics</div></div>'
        f'<div class="p-sc"><div class="p-sn">{s["consulting_years"]}</div><div class="p-sl">Yrs consulting</div></div>'
        f'</div>'
        f'<div class="p-summary">'
        f'<span class="p-summary-lede">{esc(p.get("summary_lede",""))}</span>'
        f'<span class="p-summary-rest"> {esc(p.get("summary_rest",""))}</span>'
        f'</div>'
        f'<div class="p-sec"><div class="p-st">Skills</div><div class="p-sk-wrap">{skills_html}</div></div>'
        f'<div class="p-sec"><div class="p-st">Experience</div>{exp_html}'
        f'<div class="p-note">This CV is a summary. For full task details including descriptions, challenges, inputs, outputs and outcomes — scan the QR code above to explore the interactive version.</div>'
        f'</div>'
        f'<div class="p-sec"><div class="p-st">Side Projects</div>{teaser_html}</div>'
        f'<div class="p-sec"><div class="p-st">Education</div>{edu_html}</div>'
        f'<div class="p-foot">'
        f'<span class="p-fn">{esc(p["name"])} &middot; {esc(p["role"])}</span>'
        f'<span class="p-fd">Last updated {today_str} &middot; sourced from Google Sheets &middot; powered by Python &amp; DuckDB</span>'
        f'</div>'
        f'</div>'
    )


def generate_html(d):
    p = d["person"]
    name = esc(p["name"])

    PLANE_SVG = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.8 19.2 16 11l3.5-3.5C21 6 21 4 19 2c-2-2-4-2-5.5-.5L10 5 1.8 6.2c-.5.1-.9.5-.9 1.1.1.5.4.8.9.9L6 9.1l-2.3 2.3c-.5.5-.5 1.3 0 1.8l1.1 1.1 1.1 1.1c.5.5 1.3.5 1.8 0L10 13l1.3 4.2c.1.5.5.9 1.1.9.5-.1.9-.4.9-.9l.3-1.4 1.1.8c.4.3 1 .2 1.3-.3l1.6-2.5c.5-.8.2-1.8-.6-2.2l-1.4-.7.5-2.5 1.8 7.8z"/></svg>'
    reloc_pill = (
        f'<div class="reloc">{PLANE_SVG} Open to relocation &rarr; {esc(p.get("relocation",""))}</div>'
        if p.get("relocation") and p.get("country") != "Australia"
        else ""
    )

    sidebar_html = f"""<div>
      <div class="sb-avatar">{esc(p["initials"])}</div>
      <div class="sb-name">{name}</div>
      <div class="sb-role">{esc(p["role"])}</div>
      <div class="sb-motto"><span class="sb-motto-quote">&ldquo;</span>{esc(p.get("motto",""))}<span class="sb-motto-quote">&rdquo;</span></div>
      <div class="sb-row"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg><div class="sb-txt">{esc(p.get("location",""))}{reloc_pill}</div></div>
      <div class="sb-row"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.69 13 19.79 19.79 0 0 1 1.61 4.35 2 2 0 0 1 3.6 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.91 9.91a16 16 0 0 0 6.1 6.1l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/></svg><span class="sb-txt">{esc(p.get("phone",""))}</span></div>
      <div class="sb-row"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,12 2,6"/></svg><span class="sb-txt">{esc(p.get("email",""))}</span></div>
      <div class="sb-row"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 8a6 6 0 0 1 6 6v7h-4v-7a2 2 0 0 0-2-2 2 2 0 0 0-2 2v7h-4v-7a6 6 0 0 1 6-6z"/><rect x="2" y="9" width="4" height="12"/><circle cx="4" cy="4" r="2"/></svg><a class="sb-txt" href="{esc(p.get("linkedin",""))}" target="_blank" style="color:inherit;text-decoration:none;">LinkedIn</a></div>
      <div class="sb-row"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/></svg><a class="sb-txt" href="{esc(p.get("github",""))}" target="_blank" style="color:inherit;text-decoration:none;">GitHub</a></div>
    </div>
    <hr class="sb-div">
    <div>
      <div class="sb-lbl">Citizenship &amp; work rights</div>
      <div class="badges">
        <span class="cbadge"><svg width="18" height="14" viewBox="0 0 18 14" fill="none"><polygon points="9,0.5 9.6,2.3 11.5,2.3 10.0,3.4 10.6,5.2 9,4.1 7.4,5.2 8.0,3.4 6.5,2.3 8.4,2.3" fill="#181D25"/><polygon points="9,8.5 9.4,9.8 10.8,9.8 9.7,10.6 10.1,11.9 9,11.1 7.9,11.9 8.3,10.6 7.2,9.8 8.6,9.8" fill="#181D25"/><polygon points="15.5,2.8 15.9,3.9 17.2,3.9 16.2,4.6 16.6,5.7 15.5,5.0 14.4,5.7 14.8,4.6 13.8,3.9 15.1,3.9" fill="#181D25"/><polygon points="2.5,6.2 2.9,7.6 4.2,7.6 3.2,8.3 3.6,9.4 2.5,8.7 1.4,9.4 1.8,8.3 0.8,7.6 2.1,7.6" fill="#181D25"/></svg>NZ Citizen</span>
        <span class="cbadge"><svg width="18" height="14" viewBox="0 0 18 14" fill="none"><rect x="0" y="0" width="18" height="2.4" fill="#181D25" rx="1"/><rect x="0" y="2.4" width="18" height="2" fill="#181D25" opacity="0.2"/><rect x="0" y="4.4" width="18" height="5.2" fill="#181D25" opacity="0.55"/><rect x="0" y="9.6" width="18" height="2" fill="#181D25" opacity="0.2"/><rect x="0" y="11.6" width="18" height="2.4" fill="#181D25" rx="1"/></svg>Thai Citizen</span>
      </div>
      <div class="wr-lbl">Eligible to work in</div>
      <div class="wr-pills"><span class="wr-pill">AU</span><span class="wr-pill">NZ</span><span class="wr-pill">TH</span><span class="wr-pill">SE Asia</span></div>
    </div>
    <hr class="sb-div">
    <div>
      <div class="sb-lbl">At a glance</div>
      <div class="stat-grid">
        <div class="stat-box"><div class="stat-val">{d["stats"]["total_exp"]}</div><div class="stat-lbl">Yrs experience</div></div>
        <div class="stat-box"><div class="stat-val">{d["stats"]["industries"]}</div><div class="stat-lbl">Industries</div></div>
        <div class="stat-box"><div class="stat-val">{d["stats"]["analytics_years"]}</div><div class="stat-lbl">Yrs analytics</div></div>
        <div class="stat-box"><div class="stat-val">{d["stats"]["consulting_years"]}</div><div class="stat-lbl">Yrs consulting</div></div>
      </div>
    </div>
    <hr class="sb-div">
    <div>
      <div class="sb-lbl">Key Skills</div>
      <div class="sk-row">{"".join(f'<span class="sk-pill">{esc(s)}</span>' for s in d["key_skills"])}</div>
      <div class="sk-note">Full skill breakdown available within each role.</div>
    </div>
    <hr class="sb-div">
    <div>
      <div class="sb-lbl">Navigate</div>
      <div class="nav-item active" onclick="navTo('summary',this)"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg> Summary</div>
      <div class="nav-item" onclick="navTo('skills-overview',this)"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/><line x1="2" y1="20" x2="22" y2="20"/></svg> Skills overview</div>
      <div class="nav-item" onclick="navTo('experience',this)"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="7" width="20" height="14" rx="2" ry="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg> Experience</div>
      <div class="nav-item" onclick="navTo('side-projects',this)"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg> Side projects</div>
      <div class="nav-item" onclick="navTo('education',this)"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 10v6M2 10l10-5 10 5-10 5z"/><path d="M6 12v5c3 3 9 3 12 0v-5"/></svg> Education</div>
      <div class="pdf-btn" onclick="showPDF()"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> Download CV</div>
    </div>"""

    exp_html = build_experience_html(d)
    proj_html = build_projects_html(d)
    edu_html = build_education_html(d)
    pdf_html = build_pdf_html(d)

    # Inline data as JS const — no fetch()
    cv_data_json = json.dumps(d, ensure_ascii=False, indent=None)

    js = f"""// ── DATA ─────────────────────────────────────────────────
const CV_DATA = {cv_data_json};
(function(){{var q=CV_DATA.qr_b64;if(q){{['pdf-qr-img','pdf-qr2-img'].forEach(function(id){{var el=document.getElementById(id);if(el)el.src='data:image/png;base64,'+q;}});}}}})();

// ── TREEMAP ──────────────────────────────────────────────
const tmSkills = CV_DATA.treemap_top5;
const tmTotal = tmSkills.reduce((s,x)=>s+x.tasks,0);
let row1=[],row2=[],acc=0;
for(const s of tmSkills){{
  if(acc+s.tasks<=tmTotal*0.55){{row1.push(s);acc+=s.tasks;}}
  else{{row2.push(s);}}
}}
function makeTmRow(items){{
  const row=document.createElement('div'); row.className='tm-row';
  items.forEach(s=>{{
    const cell=document.createElement('div'); cell.className='tm-cell'; cell.style.flex=`${{s.tasks}} 1 0`;
    cell.innerHTML=`<div class="tm-top"><div class="tm-name">${{s.name}}</div><div class="tm-sub">applied in</div></div><div class="tm-bottom"><div class="tm-count">${{s.tasks}}</div><div class="tm-lbl">key tasks</div></div>`;
    row.appendChild(cell);
  }});
  return row;
}}
const tm=document.getElementById('treemapChart');
tm.appendChild(makeTmRow(row1)); tm.appendChild(makeTmRow(row2));

// ── RADAR ──────────────────────────────────────────────
const radarAxes = [
  {{"label": ["Data &", "Analytics"],  "score": CV_DATA.radar_axes[0].score}},
  {{"label": ["Visuali-", "sation"],   "score": CV_DATA.radar_axes[1].score}},
  {{"label": ["AI/ML/", "Coding"],     "score": CV_DATA.radar_axes[2].score}},
  {{"label": ["CX"],                   "score": CV_DATA.radar_axes[3].score}},
  {{"label": ["CRM"],                  "score": CV_DATA.radar_axes[4].score}},
  {{"label": ["Team/", "Project"],     "score": CV_DATA.radar_axes[5].score}}
];
const RADAR_MAX = CV_DATA.radar_max;
const cobalt='#3765F6', mint='#70FC8E', N=6, RINGS=4;

function drawRadar(){{
  const canvas=document.getElementById('radarChart');
  const inner=canvas.parentElement;
  const W=inner.clientWidth, H=inner.clientHeight;
  canvas.width=W; canvas.height=H;
  const CX=W/2, CY=H/2;
  const FONT=8, LINE_H=10, GAP=4;
  const R=Math.min(CX,CY)-(GAP+2*LINE_H+6);
  const ctx=canvas.getContext('2d');
  function angle(i){{return(Math.PI*2/N)*i-Math.PI/2;}}
  function polar(r,i){{return{{x:CX+r*Math.cos(angle(i)),y:CY+r*Math.sin(angle(i))}};}}
  function hexPath(r){{ctx.beginPath();for(let i=0;i<N;i++){{const p=polar(r,i);i===0?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y);}}ctx.closePath();}}
  for(let ring=1;ring<=RINGS;ring++){{hexPath((R/RINGS)*ring);ctx.strokeStyle=cobalt;ctx.lineWidth=0.5;ctx.globalAlpha=0.2;ctx.stroke();ctx.globalAlpha=1;}}
  for(let i=0;i<N;i++){{const p=polar(R,i);ctx.beginPath();ctx.moveTo(CX,CY);ctx.lineTo(p.x,p.y);ctx.strokeStyle=cobalt;ctx.lineWidth=0.75;ctx.globalAlpha=0.2;ctx.stroke();ctx.globalAlpha=1;}}
  hexPath(R);ctx.strokeStyle=cobalt;ctx.lineWidth=2.5;ctx.globalAlpha=0.85;ctx.stroke();ctx.globalAlpha=1;
  ctx.beginPath();
  for(let i=0;i<N;i++){{const r=(radarAxes[i].score/RADAR_MAX)*R,p=polar(r,i);i===0?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y);}}
  ctx.closePath();ctx.fillStyle='rgba(112,252,142,0.75)';ctx.fill();
  ctx.font=`600 ${{FONT}}px "Geist Mono"`;ctx.fillStyle=cobalt;ctx.globalAlpha=0.9;
  for(let i=0;i<N;i++){{
    const a=angle(i),lx=CX+(R+GAP)*Math.cos(a),ly=CY+(R+GAP)*Math.sin(a);
    const lines=radarAxes[i].label,totalH=lines.length*LINE_H;
    let align=lx<CX-6?'right':lx>CX+6?'left':'center';
    let startY=i===0?ly-totalH-2:i===N/2?ly+4:ly-totalH/2;
    startY=Math.max(2,Math.min(startY,H-totalH-2));
    ctx.textAlign=align;ctx.textBaseline='top';
    lines.forEach((line,li)=>ctx.fillText(line,lx,startY+li*LINE_H));
  }}
  ctx.globalAlpha=1;
}}
document.fonts.ready.then(()=>drawRadar());

function navTo(id,el){{
  document.getElementById(id).scrollIntoView({{behavior:'smooth',block:'start'}});
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  el.classList.add('active');
}}
function toggleDetail(btn){{
  const dp=btn.closest('.task-row').querySelector('.dp');
  const isOpen=dp.style.maxHeight&&dp.style.maxHeight!=='0px';
  if(isOpen){{dp.style.maxHeight='0';dp.style.opacity='0';btn.classList.remove('open');btn.textContent='View details ↓';}}
  else{{dp.style.maxHeight=dp.scrollHeight+'px';dp.style.opacity='1';btn.classList.add('open');btn.textContent='Hide details ↑';
    dp.addEventListener('transitionend',function once(){{if(dp.style.opacity==='1')dp.style.maxHeight='none';dp.removeEventListener('transitionend',once);}});}}
}}
function toggleProjDetail(btn){{
  const dp=btn.closest('.proj-card').querySelector('.proj-dp');
  const isOpen=dp.style.maxHeight&&dp.style.maxHeight!=='0px';
  if(isOpen){{dp.style.maxHeight=dp.scrollHeight+'px';requestAnimationFrame(()=>{{dp.style.maxHeight='0';dp.style.opacity='0';}});btn.innerHTML='<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg> View details';}}
  else{{dp.style.maxHeight=dp.scrollHeight+'px';dp.style.opacity='1';btn.innerHTML='<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg> Hide details';
    dp.addEventListener('transitionend',function once(){{if(dp.style.opacity==='1')dp.style.maxHeight='none';dp.removeEventListener('transitionend',once);}});}}
}}
function toggleShowMore(id,btn,n){{
  const c=document.getElementById(id);
  const isOpen=c.style.maxHeight&&c.style.maxHeight!=='0px';
  if(isOpen){{c.style.maxHeight=c.scrollHeight+'px';requestAnimationFrame(()=>{{c.style.maxHeight='0';c.style.opacity='0';}});
    btn.innerHTML='<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg> Show '+n+' more task'+(n>1?'s':'');}}
  else{{c.style.maxHeight=c.scrollHeight+'px';c.style.opacity='1';btn.innerHTML='<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg> Show less';
    c.addEventListener('transitionend',function once(){{if(c.style.opacity==='1')c.style.maxHeight='none';c.removeEventListener('transitionend',once);}});}}
}}
function collapseSection(sectionId,btn){{
  const section=document.getElementById(sectionId);
  const isCollapsing=btn.textContent.includes('Collapse');
  section.querySelectorAll('.dp, .proj-dp').forEach(dp=>{{
    if(isCollapsing){{dp.style.maxHeight='0';dp.style.opacity='0';}}
    else{{dp.style.maxHeight=dp.scrollHeight+'px';dp.style.opacity='1';
      dp.addEventListener('transitionend',function once(){{if(dp.style.opacity==='1')dp.style.maxHeight='none';dp.removeEventListener('transitionend',once);}});}}
  }});
  section.querySelectorAll('.vd-btn').forEach(b=>{{
    if(isCollapsing){{b.classList.remove('open');b.textContent='View details ↓';}}
    else{{b.classList.add('open');b.textContent='Hide details ↑';}}
  }});
  btn.textContent=isCollapsing?'▸ Expand all':'▾ Collapse all';
}}
function showPDF(){{
  const el=document.getElementById('pdf-page');
  el.style.display='block';
  const opt={{
    margin:0,
    filename:'CV_PraphanIamsam-ang.pdf',
    image:{{type:'jpeg',quality:0.98}},
    html2canvas:{{scale:2,useCORS:true,letterRendering:true}},
    jsPDF:{{unit:'mm',format:'a4',orientation:'portrait'}}
  }};
  document.fonts.ready.then(()=>{{
    html2pdf().set(opt).from(el).save().then(()=>{{
      el.style.display='none';
    }});
  }});
}}"""

    today_str = TODAY.strftime("%d %B %Y").lstrip("0")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{name} — CV</title>
  <link href="https://fonts.googleapis.com/css2?family=Geist+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
  <style>{CSS}</style>
</head>
<body>
  <div class="shell">
    <aside class="sb">{sidebar_html}</aside>
    <main class="main">
      <div class="main-inner">

        <div id="summary">
          <div class="sec-hdr"><span class="sec-ttl">Career Summary</span></div>
          <div class="summary-card">
            <span class="summary-lede">{esc(p.get("summary_lede",""))}</span>
            <span class="summary-rest">{esc(p.get("summary_rest",""))}</span>
          </div>
        </div>

        <div id="skills-overview">
          <div class="sec-hdr">
            <span class="sec-ttl">Skills Overview</span>
            <span style="font-size:10px;color:var(--mist);">Top 5 technical &middot; key task frequency</span>
          </div>
          <div class="skills-card">
            <div class="radar-wrap">
              <div class="radar-inner"><canvas id="radarChart"></canvas></div>
            </div>
            <div class="card-divider"></div>
            <div class="treemap-wrap">
              <div class="treemap-inner" id="treemapChart"></div>
            </div>
          </div>
        </div>

        <div id="experience" class="sec-gap">
          <div class="sec-hdr">
            <span class="sec-ttl">Experience</span>
            <span class="clp-btn" onclick="collapseSection('experience',this)">&#x25B8; Expand all</span>
          </div>
          {exp_html}
        </div>

        <div id="side-projects" class="sec-gap">
          <div class="sec-hdr">
            <span class="sec-ttl">Side Projects</span>
            <span class="clp-btn" onclick="collapseSection('side-projects',this)">&#x25B8; Expand all</span>
          </div>
          {proj_html}
        </div>

        <div id="education" class="sec-gap">
          <div class="sec-hdr"><span class="sec-ttl">Education</span></div>
          {edu_html}
        </div>

        <div class="cv-footer">
          <span>Last updated {today_str} &middot; sourced from Google Sheets &middot; powered by Python &amp; DuckDB</span>
        </div>

      </div>
    </main>
  </div>
  {pdf_html}
  <script>{js}</script>
</body>
</html>"""


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        raise ValueError("GOOGLE_SHEET_ID environment variable not set")

    print("Step 1/6  Authenticating with Google...")
    gc = get_gc()

    print("Step 2/6  Fetching sheets...")
    sheets = fetch_sheets(gc, sheet_id)

    print("Step 3/6  Building cv_data...")
    cv_data = build_cv_data(sheets)

    print("Step 4/6  Generating QR code...")
    qr_b64 = make_qr_b64()
    cv_data["qr_b64"] = qr_b64

    print("Step 5/6  Generating HTML...")
    html = generate_html(cv_data)

    print("Step 6/6  Writing output files...")

    # Backup existing index.html
    index_path = DOCS_DIR / "index.html"
    if index_path.exists():
        shutil.copy(index_path, DOCS_DIR / "index.previous.html")
        print("  Backed up index.html → index.previous.html")

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    SHARED_DIR.mkdir(parents=True, exist_ok=True)

    index_path.write_text(html, encoding="utf-8")
    print(f"  Wrote docs/index.html ({len(html):,} bytes)")

    json_str = json.dumps(cv_data, ensure_ascii=False, indent=2)
    (SHARED_DIR / "cv_data.json").write_text(json_str, encoding="utf-8")
    (DOCS_DIR / "cv_data.json").write_text(json_str, encoding="utf-8")
    print(f"  Wrote shared/cv_data.json and docs/cv_data.json ({len(json_str):,} bytes)")

    print("\nBuild complete. Activate your venv and run: python pipeline/build.py")
    print("Then open docs/index.html in your browser.")
    print("When satisfied, run Agent 3.")


if __name__ == "__main__":
    main()
