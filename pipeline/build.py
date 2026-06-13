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
import pandas as pd

load_dotenv()

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
SHARED = ROOT / "shared"
DOCS.mkdir(exist_ok=True)

SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SA_JSON_STR = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SA_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

TAB_NAMES = [
    "Person", "Role", "Organization", "Role-Location", "Location",
    "Skills", "Task", "Task-Skills", "Role-Transition", "Education",
    "Education-Major", "Major", "Edu-Location", "Side Projects",
    "Side Projects - Skills",
]

# ── helpers ──────────────────────────────────────────────────────────────────

def to_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(v.strip(), fmt).date()
            except ValueError:
                continue
    return None

def tenure(from_date, to_date_val, is_current=False):
    if not from_date:
        return 0, 0
    end = date.today() if (is_current or not to_date_val) else to_date_val
    months = (end.year - from_date.year) * 12 + (end.month - from_date.month)
    months = max(months, 0)
    return months // 12, months % 12

def fmt_tenure(years, months):
    parts = []
    if years:
        parts.append(f"{years}yr")
    if months:
        parts.append(f"{months}mo")
    return " ".join(parts) if parts else "< 1mo"

def fmt_date(d):
    if not d:
        return ""
    return d.strftime("%b %Y")

def str_val(v):
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("none", "nan", "n/a") else s

def is_yes(v):
    return str_val(v).upper() == "Y"

def col(df, *names):
    for n in names:
        if n in df.columns:
            return n
    return None

# ── authentication ────────────────────────────────────────────────────────────

def authenticate():
    if SA_JSON_STR:
        info = json.loads(SA_JSON_STR)
    elif SA_FILE:
        sa_path = ROOT / SA_FILE if not Path(SA_FILE).is_absolute() else Path(SA_FILE)
        with open(sa_path, encoding="utf-8") as f:
            info = json.load(f)
    else:
        sa_path = Path(__file__).parent / "service_account.json"
        with open(sa_path, encoding="utf-8") as f:
            info = json.load(f)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)

# ── sheet loading ─────────────────────────────────────────────────────────────

def load_sheets(client):
    print(f"  Opening sheet {SHEET_ID}")
    book = client.open_by_key(SHEET_ID)
    tabs = {}
    for name in TAB_NAMES:
        print(f"    Loading tab: {name}")
        ws = book.worksheet(name)
        records = ws.get_all_records(numericise_ignore=["all"])
        tabs[name] = pd.DataFrame(records)
        print(f"      {len(tabs[name])} rows")
    return tabs

# ── QR code ───────────────────────────────────────────────────────────────────

def make_qr():
    import qrcode
    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data("https://praphani.github.io/interactive-CV")
    qr.make(fit=True)
    img = qr.make_image(fill_color="#181D25", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

# ── transformations ───────────────────────────────────────────────────────────

def build_cv_data(tabs):
    con = duckdb.connect()

    for name, df in tabs.items():
        safe = name.replace("-", "_").replace(" ", "_")
        con.register(safe, df)

    # ── person ────────────────────────────────────────────────────────────────
    p = tabs["Person"].iloc[0]
    first = str_val(p.get("First Name", ""))
    last = str_val(p.get("Last Name", ""))
    summary_raw = str_val(p.get("Career Summary", ""))
    dot = summary_raw.find(".")
    if dot >= 0:
        lede = summary_raw[: dot + 1].strip()
        body = summary_raw[dot + 1 :].strip()
    else:
        lede, body = summary_raw, ""

    p_location_id = str_val(p.get("L_ID", ""))
    loc_df = tabs["Location"]
    loc_id_col = col(loc_df, "L_ID", "id")
    city_col = col(loc_df, "City", "city", "Name", "name")
    country_col = col(loc_df, "Country", "country")
    home_city, home_country = "", ""
    if loc_id_col and city_col:
        loc_row = loc_df[loc_df[loc_id_col].astype(str) == p_location_id]
        if not loc_row.empty:
            home_city = str_val(loc_row.iloc[0].get(city_col, ""))
            home_country = str_val(loc_row.iloc[0].get(country_col, "")) if country_col else ""

    person = {
        "first_name": first,
        "last_name": last,
        "full_name": f"{first} {last}".strip(),
        "initials": (first[:1] + last[:1]).upper() if first or last else "?",
        "email": str_val(p.get("Email", "")),
        "phone": str_val(p.get("Phone", "")),
        "phone2": str_val(p.get("Phone 2", p.get("Phone2", ""))),
        "linkedin": str_val(p.get("linkedin", p.get("LinkedIn", ""))),
        "github": str_val(p.get("github", p.get("GitHub", p.get("Github", "")))),
        "motto": str_val(p.get("Motto", "")),
        "career_summary_lede": lede,
        "career_summary_body": body,
        "home_city": home_city,
        "home_country": home_country,
        "country": home_country,
        "relocation": str_val(p.get("Relocation", "")),
    }

    # ── skills ────────────────────────────────────────────────────────────────
    sk_df = tabs["Skills"].copy()
    sk_id_col = col(sk_df, "S_ID", "s_id")
    sk_name_col = col(sk_df, "Skill Name", "skill_name", "Name", "name")
    sk_cat_col = col(sk_df, "Skill Category", "skill_category", "Category", "category")
    sk_key_col = col(sk_df, "Key Skills", "key_skills", "Key_Skills")
    sk_radar_col = col(sk_df, "Radar Chart", "radar_chart", "Radar")

    key_skills = []
    if sk_key_col and sk_name_col:
        key_skills = [
            str_val(r[sk_name_col])
            for _, r in sk_df.iterrows()
            if is_yes(r.get(sk_key_col, "")) and str_val(r.get(sk_name_col, ""))
        ]

    # category normalisation
    RADAR_AXES = ["Data & Analytics", "Visualisation", "AI/ML/Coding", "CX", "CRM", "Team/Project Mgmt"]
    TECH_CATS = {"Data & Analytics", "Visualisation", "AI/ML/Coding", "CX", "CRM"}

    def norm_cat(c):
        c = str_val(c).replace(" / ", "/").replace(" /", "/").replace("/ ", "/")
        if c in ("AI/ML", "Coding", "AI/ML/Coding"):
            return "AI/ML/Coding"
        if "Team" in c or "Project Management" in c or "Project Mgmt" in c:
            return "Team/Project Mgmt"
        return c

    if sk_cat_col:
        sk_df["_cat"] = sk_df[sk_cat_col].apply(norm_cat)
    else:
        sk_df["_cat"] = ""

    # build skill lookup: S_ID → {name, cat, key, radar}
    skill_map = {}
    if sk_id_col and sk_name_col:
        for _, r in sk_df.iterrows():
            sid = str_val(r[sk_id_col])
            if sid:
                skill_map[sid] = {
                    "name": str_val(r[sk_name_col]),
                    "cat": str_val(r.get("_cat", "")),
                    "key": is_yes(r.get(sk_key_col, "")) if sk_key_col else False,
                    "radar": is_yes(r.get(sk_radar_col, "Y")) if sk_radar_col else True,
                }

    # ── task-skills lookup ────────────────────────────────────────────────────
    ts_df = tabs["Task-Skills"]
    ts_tid_col = col(ts_df, "T_ID", "t_id")
    ts_sid_col = col(ts_df, "S_ID", "s_id")

    # task → [skill_names] (distinct)
    task_skills_map = {}  # T_ID → set of skill names
    if ts_tid_col and ts_sid_col:
        for _, r in ts_df.iterrows():
            tid = str_val(r[ts_tid_col])
            sid = str_val(r[ts_sid_col])
            if tid and sid and sid in skill_map:
                task_skills_map.setdefault(tid, set()).add(skill_map[sid]["name"])

    # ── radar & treemap ───────────────────────────────────────────────────────
    # count tasks per skill (across all tasks)
    skill_task_counts = {}  # skill name → task count
    for tid, snames in task_skills_map.items():
        for sname in snames:
            skill_task_counts[sname] = skill_task_counts.get(sname, 0) + 1

    # radar: sum task counts per axis category (radar=Y skills only)
    radar_scores = {ax: 0 for ax in RADAR_AXES}
    for sid, sm in skill_map.items():
        if sm["radar"] and sm["cat"] in radar_scores:
            radar_scores[sm["cat"]] = radar_scores.get(sm["cat"], 0) + skill_task_counts.get(sm["name"], 0)

    radar_axes = [{"axis": ax, "score": radar_scores[ax]} for ax in RADAR_AXES]
    radar_max = max((r["score"] for r in radar_axes), default=1)

    # treemap: top 5 technical skills by task count
    tech_skills = [
        {"name": sm["name"], "tasks": skill_task_counts.get(sm["name"], 0)}
        for sm in skill_map.values()
        if sm["cat"] in TECH_CATS and skill_task_counts.get(sm["name"], 0) > 0
    ]
    tech_skills.sort(key=lambda x: -x["tasks"])
    treemap_top5 = tech_skills[:5]

    # ── tasks ─────────────────────────────────────────────────────────────────
    tk_df = tabs["Task"]
    tk_id_col = col(tk_df, "T_ID", "t_id")
    tk_wid_col = col(tk_df, "W_ID", "w_id")
    tk_seq_col = col(tk_df, "Task_Sequence", "Task Sequence", "sequence")
    tk_name_col = col(tk_df, "Task Name", "task_name", "name")
    tk_desc_col = col(tk_df, "Task Description", "task_description", "description")
    tk_chal_col = col(tk_df, "Challenge", "challenge")
    tk_in_col = col(tk_df, "Input", "input")
    tk_out_col = col(tk_df, "Output", "output")
    tk_outcome_col = col(tk_df, "Outcome", "outcome")

    # W_ID → list of tasks (sorted by sequence)
    role_tasks_map = {}
    if tk_wid_col and tk_id_col:
        for _, r in tk_df.iterrows():
            wid = str_val(r[tk_wid_col])
            tid = str_val(r[tk_id_col])
            if not wid or not tid:
                continue
            task = {
                "t_id": tid,
                "sequence": int(str_val(r.get(tk_seq_col, 0) or 0)) if tk_seq_col else 0,
                "name": str_val(r[tk_name_col]) if tk_name_col else "",
                "description": str_val(r[tk_desc_col]) if tk_desc_col else "",
                "challenge": str_val(r[tk_chal_col]) if tk_chal_col else "",
                "input": str_val(r[tk_in_col]) if tk_in_col else "",
                "output": str_val(r[tk_out_col]) if tk_out_col else "",
                "outcome": str_val(r[tk_outcome_col]) if tk_outcome_col else "",
                "skills": sorted(task_skills_map.get(tid, set())),
            }
            role_tasks_map.setdefault(wid, []).append(task)

    for wid in role_tasks_map:
        role_tasks_map[wid].sort(key=lambda t: t["sequence"])

    # ── locations ─────────────────────────────────────────────────────────────
    loc_df2 = tabs["Location"]
    l_id_col = col(loc_df2, "L_ID", "l_id")
    l_city_col = col(loc_df2, "City", "city", "Name", "name")
    l_country_col = col(loc_df2, "Country", "country")

    location_map = {}         # L_ID → city string
    location_country_map = {} # L_ID → country string
    if l_id_col and l_city_col:
        for _, r in loc_df2.iterrows():
            lid = str_val(r[l_id_col])
            city = str_val(r[l_city_col])
            if lid and city:
                location_map[lid] = city
            if l_country_col:
                country = str_val(r[l_country_col])
                if lid and country:
                    location_country_map[lid] = country

    rl_df = tabs["Role-Location"]
    rl_wid_col = col(rl_df, "W_ID", "w_id")
    rl_lid_col = col(rl_df, "L_ID", "l_id")

    # W_ID → [cities], Bangkok first, deduplicated
    role_locations_map = {}
    if rl_wid_col and rl_lid_col:
        for _, r in rl_df.iterrows():
            wid = str_val(r[rl_wid_col])
            lid = str_val(r[rl_lid_col])
            if wid and lid and lid in location_map:
                role_locations_map.setdefault(wid, []).append(location_map[lid])

    def order_locations(locs):
        seen = set()
        result = []
        for city in locs:
            if city not in seen:
                seen.add(city)
                result.append(city)
        # Bangkok first
        if "Bangkok" in seen:
            result.remove("Bangkok")
            result.insert(0, "Bangkok")
        return result

    # ── roles ─────────────────────────────────────────────────────────────────
    ro_df = tabs["Role"]
    ro_wid_col = col(ro_df, "W_ID", "w_id")
    ro_pid_col = col(ro_df, "P_ID", "p_id")
    ro_oid_col = col(ro_df, "O_ID", "o_id")
    ro_role_col = col(ro_df, "Role", "role", "Title", "title")
    ro_from_col = col(ro_df, "Work_From", "work_from", "From", "from")
    ro_to_col = col(ro_df, "Work_To", "work_to", "To", "to")
    ro_curr_col = col(ro_df, "Current Flag", "current_flag", "Current", "current")
    ro_anal_col = col(ro_df, "Analytics", "analytics")
    ro_cons_col = col(ro_df, "Consulting", "consulting")

    # Organization lookup
    org_df = tabs["Organization"]
    org_oid_col = col(org_df, "O_ID", "o_id")
    org_name_col = col(org_df, "Name", "name", "Company", "company", "Organisation", "Organization")
    org_ind_col = col(org_df, "Industry", "industry")

    org_map = {}  # O_ID → {name, industry}
    if org_oid_col and org_name_col:
        for _, r in org_df.iterrows():
            oid = str_val(r[org_oid_col])
            if oid:
                org_map[oid] = {
                    "name": str_val(r[org_name_col]),
                    "industry": str_val(r[org_ind_col]) if org_ind_col else "",
                }

    # Role-Transition lookup
    rt_df = tabs["Role-Transition"]
    rt_prev_col = col(rt_df, "Previous Role", "previous_role", "prev_role", "From", "from")
    rt_next_col = col(rt_df, "Next Role", "next_role", "next_role", "To", "to")
    rt_type_col = col(rt_df, "Transition Type", "transition_type", "type")

    transitions_map = {}  # (prev_wid, next_wid) → label
    if rt_prev_col and rt_next_col:
        for _, r in rt_df.iterrows():
            prev = str_val(r[rt_prev_col])
            nxt = str_val(r[rt_next_col])
            label = str_val(r[rt_type_col]) if rt_type_col else ""
            if prev and nxt:
                transitions_map[(prev, nxt)] = label or "↑ Promoted"

    # Build role objects
    roles_raw = []
    total_months = 0
    analytics_months = 0
    consulting_months = 0
    industries_seen = set()

    for _, r in ro_df.iterrows():
        wid = str_val(r[ro_wid_col]) if ro_wid_col else ""
        if not wid:
            continue
        oid = str_val(r[ro_oid_col]) if ro_oid_col else ""
        is_curr = is_yes(r.get(ro_curr_col, "")) if ro_curr_col else False
        fd = to_date(r[ro_from_col]) if ro_from_col else None
        td = to_date(r[ro_to_col]) if ro_to_col else None
        ty, tm = tenure(fd, td, is_curr)
        role_months = ty * 12 + tm
        total_months += role_months

        if ro_anal_col and is_yes(r.get(ro_anal_col, "")):
            analytics_months += role_months
        if ro_cons_col and is_yes(r.get(ro_cons_col, "")):
            consulting_months += role_months

        org = org_map.get(oid, {"name": "", "industry": ""})
        if org["industry"]:
            industries_seen.add(org["industry"])

        locs = order_locations(role_locations_map.get(wid, []))
        tasks = role_tasks_map.get(wid, [])

        # Role-level skills = union of all task skills
        role_skills = sorted({s for t in tasks for s in t["skills"]})

        roles_raw.append({
            "w_id": wid,
            "o_id": oid,
            "title": str_val(r[ro_role_col]) if ro_role_col else "",
            "is_current": is_curr,
            "date_from_obj": fd,
            "date_to_obj": td,
            "tenure_years": ty,
            "tenure_months": tm,
            "tenure_str": fmt_tenure(ty, tm),
            "date_from": fmt_date(fd),
            "date_to": "Present" if is_curr else fmt_date(td),
            "org_name": org["name"],
            "industry": org["industry"],
            "locations": locs,
            "skills": role_skills,
            "tasks": tasks,
        })

    # Group roles by company (O_ID), sorted by most recent first within group
    from itertools import groupby
    company_groups = {}
    for role in roles_raw:
        company_groups.setdefault(role["o_id"], []).append(role)

    # Sort roles within each company: current first, then by date_from descending
    for oid in company_groups:
        company_groups[oid].sort(
            key=lambda r: (not r["is_current"], -(r["date_from_obj"] or date.min).toordinal())
        )

    # Build company entries, sort companies by most recent role date desc
    experience = []
    for oid, comp_roles in company_groups.items():
        all_dates = [r["date_from_obj"] for r in comp_roles if r["date_from_obj"]]
        all_to_dates = [r["date_to_obj"] for r in comp_roles if r["date_to_obj"]]
        is_curr_company = any(r["is_current"] for r in comp_roles)
        comp_months = sum(r["tenure_years"] * 12 + r["tenure_months"] for r in comp_roles)
        comp_ty, comp_tm = comp_months // 12, comp_months % 12
        comp_from = min(all_dates) if all_dates else None
        comp_to = max(all_to_dates) if (all_to_dates and not is_curr_company) else None

        # locations across all roles, deduplicated/Bangkok first
        all_locs_flat = [loc for r in comp_roles for loc in r["locations"]]
        comp_locs = order_locations(all_locs_flat)

        # Transitions between roles (ordered by date desc within company)
        comp_transitions = []
        sorted_by_date = sorted(
            comp_roles,
            key=lambda r: (r["date_from_obj"] or date.min).toordinal()
        )
        for i in range(len(sorted_by_date) - 1):
            older = sorted_by_date[i]
            newer = sorted_by_date[i + 1]
            label = transitions_map.get(
                (older["w_id"], newer["w_id"]),
                transitions_map.get((newer["w_id"], older["w_id"]), "↑ Promoted")
            )
            comp_transitions.append({
                "from_w_id": older["w_id"],
                "to_w_id": newer["w_id"],
                "label": label,
            })

        # Remove internal keys not needed in JSON
        roles_clean = []
        for r in comp_roles:
            rc = {k: v for k, v in r.items() if k not in ("date_from_obj", "date_to_obj", "o_id", "org_name", "industry", "locations")}
            roles_clean.append(rc)

        experience.append({
            "o_id": oid,
            "company": comp_roles[0]["org_name"],
            "industry": comp_roles[0]["industry"],
            "locations": comp_locs,
            "is_current": is_curr_company,
            "total_tenure_years": comp_ty,
            "total_tenure_months": comp_tm,
            "total_tenure_str": fmt_tenure(comp_ty, comp_tm),
            "date_from": fmt_date(comp_from),
            "date_to": "Present" if is_curr_company else fmt_date(comp_to),
            "roles": roles_clean,
            "transitions": comp_transitions,
            "_sort_key": (is_curr_company, (comp_from or date.min).toordinal()),
        })

    experience.sort(key=lambda e: (-int(e["_sort_key"][0]), -e["_sort_key"][1]))
    for e in experience:
        del e["_sort_key"]

    # ── stats ─────────────────────────────────────────────────────────────────
    stats = {
        "total_exp": total_months // 12,
        "industries": len(industries_seen),
        "analytics_years": analytics_months // 12,
        "consulting_years": consulting_months // 12,
    }

    # ── side projects ─────────────────────────────────────────────────────────
    sp_df = tabs["Side Projects"]
    sp_id_col = col(sp_df, "SP_ID", "sp_id", "ID", "id")
    sp_name_col = col(sp_df, "Name", "name")
    sp_ord_col = col(sp_df, "Display Order Sequence", "display_order_sequence", "display_order", "Order", "order")
    sp_desc_col = col(sp_df, "Description", "description")
    sp_chal_col = col(sp_df, "Challenge", "challenge")
    sp_built_col = col(sp_df, "Built with", "built_with", "Built With")
    sp_out_col = col(sp_df, "Output", "output")
    sp_comp_col = col(sp_df, "Completed On", "completed_on")
    sp_url_col = col(sp_df, "URL", "url")

    sps_df = tabs["Side Projects - Skills"]
    sps_spid_col = col(sps_df, "SP_ID", "sp_id")
    sps_sid_col = col(sps_df, "S_ID", "s_id")

    sp_skills_map = {}  # SP_ID → [skill names]
    if sps_spid_col and sps_sid_col:
        for _, r in sps_df.iterrows():
            spid = str_val(r[sps_spid_col])
            sid = str_val(r[sps_sid_col])
            if spid and sid and sid in skill_map:
                sp_skills_map.setdefault(spid, set()).add(skill_map[sid]["name"])

    side_projects = []
    for _, r in sp_df.iterrows():
        spid = str_val(r[sp_id_col]) if sp_id_col else ""
        name = str_val(r[sp_name_col]) if sp_name_col else ""
        if not name:
            continue
        try:
            order = int(str_val(r[sp_ord_col]) or 999) if sp_ord_col else 999
        except (ValueError, TypeError):
            order = 999
        side_projects.append({
            "sp_id": spid,
            "name": name,
            "order": order,
            "description": str_val(r[sp_desc_col]) if sp_desc_col else "",
            "challenge": str_val(r[sp_chal_col]) if sp_chal_col else "",
            "built_with": str_val(r[sp_built_col]) if sp_built_col else "",
            "output": str_val(r[sp_out_col]) if sp_out_col else "",
            "completed_on": str_val(r[sp_comp_col]) if sp_comp_col else "",
            "url": str_val(r[sp_url_col]) if sp_url_col else "",
            "skills": sorted(sp_skills_map.get(spid, set())),
        })

    side_projects.sort(key=lambda s: s["order"])

    # PDF teaser
    names = [sp["name"] for sp in side_projects]
    if len(names) > 3:
        sp_teaser = ", ".join(names[:3]) + f", and {len(names) - 3} more"
    elif len(names) > 1:
        sp_teaser = ", ".join(names[:-1]) + f" and {names[-1]}"
    elif names:
        sp_teaser = names[0]
    else:
        sp_teaser = ""

    # ── education ─────────────────────────────────────────────────────────────
    ed_df = tabs["Education"]
    ed_id_col = col(ed_df, "E_ID", "e_id")
    ed_pid_col = col(ed_df, "P_ID", "p_id")
    ed_type_col = col(ed_df, "Type", "type")
    ed_inst_col = col(ed_df, "Institute", "institute")
    ed_deg_col = col(ed_df, "Degree / Certificate", "degree_certificate", "Degree", "degree", "Certificate", "certificate")
    ed_skill_col = col(ed_df, "Skill", "skill")
    ed_from_col = col(ed_df, "From_Date", "from_date", "From", "from")
    ed_to_col = col(ed_df, "To_Date", "to_date", "To", "to")
    ed_ord_col = col(ed_df, "Display Order", "display_order", "Order", "order")

    em_df = tabs["Education-Major"]
    em_eid_col = col(em_df, "E_ID", "e_id")
    em_mid_col = col(em_df, "M_ID", "m_id")

    maj_df = tabs["Major"]
    maj_id_col = col(maj_df, "M_ID", "m_id")
    maj_name_col = col(maj_df, "Major", "major", "Name", "name")

    major_map = {}  # M_ID → name
    if maj_id_col and maj_name_col:
        for _, r in maj_df.iterrows():
            mid = str_val(r[maj_id_col])
            if mid:
                major_map[mid] = str_val(r[maj_name_col])

    ed_majors_map = {}  # E_ID → [major names]
    if em_eid_col and em_mid_col:
        for _, r in em_df.iterrows():
            eid = str_val(r[em_eid_col])
            mid = str_val(r[em_mid_col])
            if eid and mid and mid in major_map:
                ed_majors_map.setdefault(eid, []).append(major_map[mid])

    el_df = tabs["Edu-Location"]
    el_eid_col = col(el_df, "E_ID", "e_id")
    el_lid_col = col(el_df, "L_ID", "l_id")
    el_city_col = col(el_df, "City", "city")

    ed_location_map = {}  # E_ID → city string
    ed_country_map = {}   # E_ID → country string
    if el_eid_col:
        if el_lid_col:
            for _, r in el_df.iterrows():
                eid = str_val(r[el_eid_col])
                lid = str_val(r[el_lid_col])
                if eid and lid:
                    if lid in location_map:
                        ed_location_map[eid] = location_map[lid]
                    if lid in location_country_map:
                        ed_country_map[eid] = location_country_map[lid]
        elif el_city_col:
            for _, r in el_df.iterrows():
                eid = str_val(r[el_eid_col])
                city = str_val(r[el_city_col])
                if eid and city:
                    ed_location_map[eid] = city

    education = []
    for _, r in ed_df.iterrows():
        eid = str_val(r[ed_id_col]) if ed_id_col else ""
        inst = str_val(r[ed_inst_col]) if ed_inst_col else ""
        if not inst:
            continue
        fd = to_date(r[ed_from_col]) if ed_from_col else None
        td = to_date(r[ed_to_col]) if ed_to_col else None
        try:
            disp_ord = int(str_val(r[ed_ord_col]) or 999) if ed_ord_col else 999
        except (ValueError, TypeError):
            disp_ord = 999

        grad_year = f"'{td.strftime('%y')}" if td else ""

        ed_city = ed_location_map.get(eid, "")
        ed_country_i = ed_country_map.get(eid, "")
        education.append({
            "e_id": eid,
            "type": str_val(r[ed_type_col]) if ed_type_col else "",
            "institute": inst,
            "degree": str_val(r[ed_deg_col]) if ed_deg_col else "",
            "skill": str_val(r[ed_skill_col]) if ed_skill_col else "",
            "majors": ed_majors_map.get(eid, []),
            "city": ed_city,
            "country": ed_country_i,
            "location": ", ".join(filter(None, [ed_city, ed_country_i])),
            "from_date": fmt_date(fd),
            "to_date": fmt_date(td),
            "from_year": str(fd.year) if fd else "",
            "to_year": str(td.year) if td else "",
            "grad_year": grad_year,
            "display_order": disp_ord,
        })

    education.sort(key=lambda e: e["display_order"])

    today = date.today()
    generated_date = today.strftime(f"{today.day} %B %Y")

    return {
        "person": person,
        "stats": stats,
        "key_skills": key_skills,
        "radar_axes": radar_axes,
        "radar_max": radar_max,
        "treemap_top5": treemap_top5,
        "experience": experience,
        "side_projects": side_projects,
        "sp_teaser": sp_teaser,
        "education": education,
        "generated_date": generated_date,
    }

# ── HTML template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CV — Praphan Iamsam-ang</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Geist+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#F2F3F6;--card:#fff;--obs:#181D25;--mist:#606E80;
  --cobalt:#3765F6;--mint:#70FC8E;--border:rgba(24,29,37,0.07);
  --bm:rgba(24,29,37,0.11);
  --purple:#3C3489;--purple-bg:#EEEDFE;
  --tint:#EEF2FF;--mist-lt:#E8EAEC;
  --cm:rgba(255,255,255,0.55);--cs:rgba(255,255,255,0.1);--cb:rgba(255,255,255,0.12);
  --mono:"Geist Mono",monospace;
}
body{font-family:var(--mono);background:var(--bg);color:var(--obs);font-size:13px;line-height:1.6;-webkit-font-smoothing:antialiased}

/* ── Shell ─────────────────────────────────────── */
.shell{background:var(--bg);display:grid;grid-template-columns:300px 1fr;min-height:100vh;font-family:var(--mono)}

/* ── Sidebar ───────────────────────────────────── */
.sb{background:var(--cobalt);padding:1.5rem 1.25rem;display:flex;flex-direction:column;gap:1.2rem;position:sticky;top:0;height:100dvh;overflow-y:auto;scrollbar-width:none}
.sb::-webkit-scrollbar{display:none}
.sb svg{flex-shrink:0}
.sb-avatar{width:72px;height:72px;border-radius:50%;background:rgba(255,255,255,0.15);display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:700;color:#fff;margin-bottom:5px;flex-shrink:0}
.sb-name{font-family:var(--mono);font-size:20px;font-weight:700;color:#fff;margin-bottom:2px}
.sb-role{font-size:16px;color:var(--cm);margin-bottom:8px}
.sb-motto{font-size:11px;font-style:italic;color:var(--cm);line-height:1.6;margin-top:12px;margin-bottom:25px;opacity:0.85}
.sb-motto-quote{color:var(--mint);font-style:normal;font-weight:700}
.sb-div{border:none;border-top:0.5px solid var(--cb)}
.sb-lbl{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;color:var(--cm);margin-bottom:8px}
.sb-row{display:flex;align-items:flex-start;gap:6px;margin-bottom:5px;color:var(--cm)}
.sb-row svg{margin-top:1px;opacity:0.7;flex-shrink:0}
.sb-txt{font-size:12px;color:var(--cm);line-height:1.5}
.sb-txt a{color:var(--cm);text-decoration:none}
.sb-txt a:hover{color:#fff}
.reloc{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:10px;padding:2px 7px;border-radius:4px;background:rgba(255,255,255,0.12);color:rgba(255,255,255,0.75);margin-top:4px}

/* citizenship */
.badges{display:flex;flex-direction:column;gap:5px;margin-bottom:8px}
.cbadge{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;font-weight:700;padding:4px 9px;border-radius:4px;background:var(--mint);color:var(--obs);align-self:flex-start}
.wr-lbl{font-size:11px;color:var(--cm);margin-bottom:4px}
.wr-pills{display:flex;flex-wrap:wrap;gap:3px}
.wr-pill{font-family:var(--mono);font-size:10px;padding:2px 6px;border-radius:4px;background:rgba(255,255,255,0.15);color:#fff}

/* stats */
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.stat-box{background:var(--cs);border-radius:10px;padding:8px 10px}
.stat-val{font-family:var(--mono);font-size:20px;font-weight:700;color:#fff;line-height:1}
.stat-lbl{font-size:10px;color:var(--cm);margin-top:3px;line-height:1.3}

/* key skills */
.sk-row{display:flex;flex-wrap:wrap;gap:4px}
.sk-pill{font-size:11px;padding:3px 8px;border-radius:20px;font-family:var(--mono);background:rgba(255,255,255,0.18);color:#fff}
.sk-note{font-size:10px;color:rgba(255,255,255,0.4);margin-top:6px;font-style:italic;line-height:1.4}

/* nav */
.nav-item{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--cm);padding:6px 8px;border-radius:8px;cursor:pointer;margin-bottom:2px;user-select:none}
.nav-item svg{opacity:0.6}
.nav-item.active{background:rgba(255,255,255,0.18);color:#fff}
.nav-item.active svg{opacity:1;color:var(--mint)}
.pdf-btn{display:flex;align-items:center;justify-content:center;gap:6px;font-family:var(--mono);font-size:11px;font-weight:600;color:var(--cobalt);background:#fff;border-radius:8px;padding:8px 12px;cursor:pointer;margin-top:4px;border:none}

/* ── Main ──────────────────────────────────────── */
.main{padding:1.5rem;display:flex;flex-direction:column;gap:12px;background:var(--bg)}
.main-inner{max-width:640px;min-width:550px;width:100%;display:flex;flex-direction:column;gap:12px}

/* section titles */
.sec-ttl{font-family:var(--mono);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.1em;color:var(--mist)}
.sec-gap{margin-top:8px}

/* ── Career Summary ────────────────────────────── */
.summary-card{background:var(--card);border:0.5px solid var(--bm);border-left:3px solid var(--cobalt);border-radius:0 14px 14px 0;padding:1.1rem 1.25rem;margin-bottom:10px}
.summary-lede{font-size:14px;font-weight:600;color:var(--obs);line-height:1.6;margin-bottom:8px;display:block}
.summary-rest{font-size:12px;color:var(--mist);line-height:1.8;display:block}

/* ── Skills Overview ───────────────────────────── */
.skills-card{background:var(--card);border:0.5px solid var(--border);border-radius:14px;height:200px;display:flex;overflow:hidden;margin-bottom:1.25rem}
.skills-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:0.75rem}
.skills-note{font-size:10px;color:var(--mist)}
.radar-wrap{flex:2;display:flex;flex-direction:column;padding:12px 12px 12px 14px;background:var(--tint)}
.treemap-wrap{flex:3;display:flex;flex-direction:column;padding:12px 14px 12px 12px;border-left:0.5px solid rgba(24,29,37,0.08)}
#radarChart{flex:1;display:block;width:100%;height:100%}
#treemapChart{flex:1;display:flex;flex-direction:column;gap:3px;overflow:hidden}
.tm-row{display:flex;gap:3px;flex:1}
.tm-cell{background:var(--tint);border-radius:4px;display:flex;flex-direction:column;justify-content:space-between;padding:6px 8px;overflow:hidden;flex-shrink:0}
.tm-top{display:flex;flex-direction:column;gap:2px}
.tm-name{font-size:11px;font-weight:700;color:var(--cobalt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tm-sub{font-size:9px;color:rgba(55,101,246,0.6)}
.tm-bottom{display:flex;align-items:baseline;gap:3px}
.tm-count{font-size:24px;font-weight:700;color:rgba(55,101,246,0.85);line-height:1}
.tm-lbl{font-size:9px;color:rgba(55,101,246,0.6)}

/* ── Experience / Section ─────────────────────── */
.section{margin-bottom:1.75rem;scroll-margin-top:1.5rem}

/* cards */
.card{background:var(--card);border:0.5px solid var(--bm);border-radius:14px;padding:1.1rem 1.1rem 0.5rem;margin-bottom:10px}
.sec-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
.clp-btn{font-size:11px;color:var(--mist);display:flex;align-items:center;gap:3px;cursor:pointer;user-select:none;background:none;border:none;font-family:var(--mono)}

/* single role card */
.card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:2px}
.card-title{font-family:var(--mono);font-size:14px;font-weight:700;color:var(--obs)}
.card-company{font-family:var(--mono);font-size:13px;font-weight:700;color:var(--cobalt);margin-top:2px}
.card-meta{font-size:12px;color:var(--mist);margin-top:1px;margin-bottom:10px}
.badge-current{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:20px;background:var(--obs);color:var(--mint);font-weight:700;white-space:nowrap}
.tenure-block{text-align:right;flex-shrink:0;display:flex;flex-direction:column;align-items:flex-end;gap:4px}
.tenure-num{font-family:var(--mono);font-size:26px;font-weight:700;color:var(--obs);line-height:1}
.tenure-num span{font-size:14px;color:var(--mist);font-weight:500}
.tenure-dates{font-family:var(--mono);font-size:10px;color:var(--mist)}
.chip-row{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:10px}
.chip{font-size:11px;padding:2px 8px;border-radius:4px;font-family:var(--mono);background:#EEF2FF;color:#3765F6}
.tasks-lbl{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;color:var(--mist);margin-bottom:6px;margin-top:2px}

/* task list */
.task-row{border-top:0.5px solid var(--border);padding:9px 0}
.task-line{display:flex;align-items:flex-start;gap:7px}
.tdot{width:4px;height:4px;border-radius:50%;background:var(--cobalt);opacity:0.35;margin-top:8px;flex-shrink:0}
.ttxt{font-size:12px;color:var(--mist);line-height:1.5;flex:1}
.vd-btn{flex-shrink:0;font-size:11px;font-family:var(--mono);color:var(--cobalt);border:0.5px solid var(--cobalt);border-radius:4px;padding:2px 8px;background:transparent;cursor:pointer;white-space:nowrap;user-select:none}
.vd-btn.open{background:#EEF2FF}
.task-chip-row{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px}
.task-chip{font-size:10px;padding:2px 7px;border-radius:4px;font-family:var(--mono);background:#EEF2FF;color:#3765F6}
.dp{overflow:hidden;max-height:0;opacity:0;transition:max-height 0.35s ease,opacity 0.25s}
.dp-inner{padding:8px 0 6px 11px}
.task-desc{font-size:12px;color:var(--obs);line-height:1.6;margin-bottom:8px;padding:8px 10px;background:var(--bg);border-radius:8px;border-left:2px solid var(--cobalt)}
.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.dpc{background:var(--bg);border-radius:10px;padding:9px 10px}
.dpl{font-family:var(--mono);font-size:9px;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;color:var(--mist);margin-bottom:4px;display:flex;align-items:center;gap:4px}
.dpv{font-size:12px;color:var(--obs);line-height:1.45}
.dpc.oc{background:var(--mint)}
.dpc.oc .dpl{color:var(--obs);opacity:0.6}
.dpc.oc .dpv{color:var(--obs);font-family:var(--mono);font-size:12px;font-weight:600}
.extra-tasks{overflow:hidden;max-height:0;opacity:0;transition:max-height 0.4s ease,opacity 0.3s}
.show-more-row{border-top:0.5px solid var(--border);padding:9px 0 6px}
.show-more-btn{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-family:var(--mono);color:var(--mist);border:0.5px solid #606E80;border-radius:4px;padding:3px 10px;cursor:pointer;user-select:none;background:transparent}
.show-more-btn:hover{background:#E8EAEC}

/* multi-role company card */
.company-card{background:var(--card);border:0.5px solid var(--bm);border-radius:14px;padding:1rem 1.1rem;margin-bottom:10px}
.company-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:12px;padding-bottom:10px;border-bottom:0.5px solid var(--border)}
.company-name{font-family:var(--mono);font-size:14px;font-weight:700;color:var(--cobalt);margin-bottom:2px}
.company-meta{font-size:12px;color:var(--mist)}
.role-block{border-left:2px solid var(--border);margin-left:4px;padding-left:14px;margin-bottom:14px}
.role-block:last-child{margin-bottom:0}
.role-block.current{border-left-color:var(--mint)}
.role-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:4px}
.role-title{font-family:var(--mono);font-size:13px;font-weight:700;color:var(--obs)}
.role-tenure-num{font-family:var(--mono);font-size:18px;font-weight:700;color:var(--obs);line-height:1}
.role-tenure-num span{font-size:11px;color:var(--mist);font-weight:500}
.role-dates{font-family:var(--mono);font-size:10px;color:var(--mist);text-align:right;margin-top:2px}
.promo-row{display:flex;align-items:center;gap:6px;margin:10px 0 10px -18px;padding-left:4px}
.promo-line{flex:1;height:0.5px;background:var(--border)}
.promo-badge{font-family:var(--mono);font-size:9px;font-weight:600;color:var(--mist);background:var(--bg);padding:2px 8px;border-radius:20px;border:0.5px solid var(--border);white-space:nowrap}

/* side projects */
.proj-card{background:var(--card);border:0.5px solid var(--bm);border-radius:14px;padding:1.1rem;margin-bottom:10px}
.proj-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:4px}
.proj-title{font-family:var(--mono);font-size:14px;font-weight:700;color:var(--obs)}
.proj-meta{font-size:12px;color:var(--mist);margin-top:2px;margin-bottom:6px}
.proj-desc{font-size:12px;color:var(--mist);margin-bottom:10px;line-height:1.5}
.proj-btns{display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex-shrink:0}
.proj-link{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:10px;color:var(--cobalt);border:0.5px solid var(--cobalt);border-radius:4px;padding:3px 8px;text-decoration:none;white-space:nowrap;cursor:pointer}
.proj-dp{overflow:hidden;max-height:0;opacity:0;transition:max-height 0.35s ease,opacity 0.25s}
.proj-dp-inner{padding:8px 0 4px 0}
.proj-detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}

/* education */
.edu-card{background:var(--card);border:0.5px solid var(--bm);border-radius:14px;padding:1rem 1.1rem;margin-bottom:10px}
.edu-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:10px}
.edu-left{flex:1}
.edu-deg{font-family:var(--mono);font-size:14px;font-weight:700;color:var(--obs);margin-bottom:3px}
.edu-inst{font-size:12px;color:var(--mist);margin-bottom:6px}
.edu-meta{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.edu-lvl{font-family:var(--mono);font-size:10px;font-weight:600;padding:2px 8px;border-radius:4px;background:#E8EAEC;color:#606E80}
.edu-loc{display:flex;align-items:center;gap:3px;font-size:11px;color:var(--mist)}
.mjr-row{display:flex;align-items:center;flex-wrap:wrap;gap:5px}
.mjr-lbl{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:0.07em;text-transform:uppercase;color:var(--mist);flex-shrink:0}
.mjr-chip{font-size:11px;padding:2px 8px;border-radius:4px;font-family:var(--mono);background:#EEEDFE;color:#3C3489}
.edu-yr{font-family:var(--mono);font-size:22px;font-weight:700;color:var(--obs);line-height:1;text-align:right}
.edu-yr-range{font-family:var(--mono);font-size:10px;color:var(--mist);text-align:right;margin-top:2px}
.edu-div{border:none;border-top:0.5px solid var(--border);margin:8px 0}
.edu-pills{display:flex;flex-wrap:wrap;gap:4px}
.edu-pill{font-size:11px;padding:2px 8px;border-radius:4px;font-family:var(--mono);background:#EEF2FF;color:#3765F6}
.cert-badge{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:20px;background:var(--obs);color:var(--mint);font-weight:700;white-space:nowrap}

/* footer */
.cv-footer{font-size:10px;color:var(--mist);padding:1rem 0 2rem;text-align:right}

/* ── PDF ─────────────────────────────────────────── */
#pdf-page{display:none;font-family:var(--mono);background:#fff;padding:28px 32px;max-width:800px;color:var(--obs)}
.pdf-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}
.pdf-name{font-size:22px;font-weight:700;color:var(--obs)}
.pdf-role{font-size:11px;font-weight:600;color:var(--cobalt);margin-top:3px}
.pdf-contacts{margin-top:6px;display:flex;flex-wrap:wrap;gap:8px}
.pdf-contact{font-size:11px;color:var(--mist)}
.pdf-qr img{width:72px;height:72px}
.pdf-divider{height:2px;background:var(--cobalt);margin:12px 0}
.pdf-stats{display:flex;gap:8px;margin-bottom:16px}
.pdf-stat{flex:1;background:var(--cobalt);color:#fff;border-radius:8px;padding:8px 12px}
.pdf-stat-num{font-size:20px;font-weight:700}
.pdf-stat-lbl{font-size:10px;opacity:0.7;margin-top:2px}
.pdf-sec-ttl{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;color:var(--mist);margin:14px 0 6px}
.pdf-lede{font-size:12px;font-weight:600;color:var(--obs);margin-bottom:4px}
.pdf-body{font-size:11px;color:var(--mist);line-height:1.6;margin-bottom:10px}
.pdf-chips{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:12px}
.pdf-chip{font-size:9px;padding:2px 8px;border-radius:10px;border:0.5px solid rgba(24,29,37,0.1);background:#fff;color:var(--mist)}
.pdf-role-block{margin-bottom:10px}
.pdf-role-top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.pdf-role-title{font-size:12px;font-weight:700;color:var(--obs)}
.pdf-role-tenure{font-size:10px;color:var(--mist)}
.pdf-co{font-size:12px;font-weight:700;color:var(--cobalt)}
.pdf-meta{font-size:10px;color:var(--mist);margin-bottom:4px}
.p-tlist{list-style:none;padding:0}
.p-tlist li{padding-left:12px;position:relative;margin-bottom:5px}
.p-tlist li:last-child{margin-bottom:0}
.p-tlist li::before{content:"–";position:absolute;left:0;color:var(--cobalt);font-weight:700;font-size:10px;top:1px}
.p-tname{font-size:11px;font-weight:600;color:var(--obs);line-height:1.4}
.p-note{margin-top:10px;font-size:9px;color:var(--mist);font-style:italic;padding:7px 12px;background:#F2F3F6;border-radius:4px;border-left:2px solid var(--cobalt)}
.pdf-sp-panel{background:var(--bg);border-radius:8px;padding:12px 16px;display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}
.pdf-sp-text{font-size:11px;color:var(--mist);line-height:1.6;max-width:70%}
.pdf-sp-text strong{color:var(--obs)}
.pdf-qr2 img{width:88px;height:88px}
.pdf-edu-entry{margin-bottom:8px}
.pdf-edu-top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.pdf-edu-inst{font-size:11px;font-weight:700;color:var(--obs)}
.pdf-edu-deg{font-size:10px;color:var(--mist)}
.pdf-badge-cert{font-size:9px;font-weight:600;padding:2px 8px;border-radius:20px;background:var(--obs);color:var(--mint)}
.pdf-footer{display:flex;justify-content:space-between;font-size:9px;color:var(--mist);margin-top:16px;border-top:0.5px solid var(--border);padding-top:8px}

@media print{
  .shell{display:none!important}
  #pdf-page{display:block!important;padding:20px 24px}
  body{background:#fff}
}
</style>
</head>
<body>

<!-- ══ WEB ══════════════════════════════════════════════════════════════════ -->
<div class="shell">
  <aside class="sb" id="sb">
    <div>
      <div class="sb-avatar" id="sb-avatar"></div>
      <div class="sb-name" id="sb-name"></div>
      <div class="sb-role" id="sb-role"></div>
      <div class="sb-motto" id="sb-motto"></div>
      <div id="sb-location"></div>
      <div id="sb-contacts"></div>
    </div>
    <hr class="sb-div">
    <div>
      <div class="sb-lbl">Citizenship &amp; work rights</div>
      <div class="badges">
        <span class="cbadge"><svg width="18" height="14" viewBox="0 0 18 14" fill="none"><polygon points="9,0.5 9.6,2.3 11.5,2.3 10.0,3.4 10.6,5.2 9,4.1 7.4,5.2 8.0,3.4 6.5,2.3 8.4,2.3" fill="#181D25"/><polygon points="9,8.5 9.4,9.8 10.8,9.8 9.7,10.6 10.1,11.9 9,11.1 7.9,11.9 8.3,10.6 7.2,9.8 8.6,9.8" fill="#181D25"/><polygon points="15.5,2.8 15.9,3.9 17.2,3.9 16.2,4.6 16.6,5.7 15.5,5.0 14.4,5.7 14.8,4.6 13.8,3.9 15.1,3.9" fill="#181D25"/><polygon points="2.5,6.2 2.9,7.6 4.2,7.6 3.2,8.3 3.6,9.4 2.5,8.7 1.4,9.4 1.8,8.3 0.8,7.6 2.1,7.6" fill="#181D25"/></svg>NZ Citizen</span>
        <span class="cbadge"><svg width="18" height="14" viewBox="0 0 18 14" fill="none"><rect x="0" y="0" width="18" height="2.4" fill="#181D25" rx="1"/><rect x="0" y="2.4" width="18" height="2" fill="#181D25" opacity="0.2"/><rect x="0" y="4.4" width="18" height="5.2" fill="#181D25" opacity="0.55"/><rect x="0" y="9.6" width="18" height="2" fill="#181D25" opacity="0.2"/><rect x="0" y="11.6" width="18" height="2.4" fill="#181D25" rx="1"/></svg>Thai Citizen</span>
      </div>
      <div class="wr-lbl">Eligible to work in</div>
      <div class="wr-pills">
        <span class="wr-pill">AU</span>
        <span class="wr-pill">NZ</span>
        <span class="wr-pill">TH</span>
        <span class="wr-pill">SE Asia</span>
      </div>
    </div>
    <hr class="sb-div">
    <div>
      <div class="sb-lbl">At a glance</div>
      <div class="stat-grid" id="stat-grid"></div>
    </div>
    <hr class="sb-div">
    <div>
      <div class="sb-lbl">Key Skills</div>
      <div class="sk-row" id="key-skills-wrap"></div>
      <div class="sk-note">Full skill breakdown available within each role.</div>
    </div>
    <hr class="sb-div">
    <div>
      <div class="sb-lbl">Navigate</div>
      <div class="nav-item active" id="nav-summary" onclick="navTo('summary',this)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
        Summary
      </div>
      <div class="nav-item" id="nav-skills" onclick="navTo('skills-overview',this)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
        Skills overview
      </div>
      <div class="nav-item" id="nav-exp" onclick="navTo('experience',this)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/></svg>
        Experience
      </div>
      <div class="nav-item" id="nav-proj" onclick="navTo('side-projects',this)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
        Side projects
      </div>
      <div class="nav-item" id="nav-edu" onclick="navTo('education',this)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
        Education
      </div>
      <div class="pdf-btn" onclick="showPDF()">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        Download CV
      </div>
    </div>
  </aside>

  <div class="main">
    <div class="main-inner">

      <!-- Career Summary -->
      <div class="section" id="summary">
        <div class="sec-hdr"><div class="sec-ttl">Career Summary</div></div>
        <div class="summary-card">
          <span class="summary-lede" id="sum-lede"></span>
          <span class="summary-rest" id="sum-body"></span>
        </div>
      </div>

      <!-- Skills Overview -->
      <div class="section" id="skills-overview">
        <div class="sec-hdr">
          <div class="sec-ttl">Skills Overview</div>
          <div class="skills-note">Top 5 technical &middot; key task frequency</div>
        </div>
        <div class="skills-card">
          <div class="radar-wrap">
            <canvas id="radarChart"></canvas>
          </div>
          <div class="treemap-wrap">
            <div id="treemapChart"></div>
          </div>
        </div>
      </div>

      <!-- Experience -->
      <div class="section sec-gap" id="experience">
        <div class="sec-hdr">
          <div class="sec-ttl">Experience</div>
          <button class="clp-btn" id="btn-exp-expand" onclick="collapseSection('experience', this)">&#9656; Expand all</button>
        </div>
        <div id="exp-cards"></div>
      </div>

      <!-- Side Projects -->
      <div class="section sec-gap" id="side-projects">
        <div class="sec-hdr">
          <div class="sec-ttl">Side Projects</div>
          <button class="clp-btn" id="btn-proj-expand" onclick="collapseSection('side-projects', this)">&#9656; Expand all</button>
        </div>
        <div id="proj-cards"></div>
      </div>

      <!-- Education -->
      <div class="section sec-gap" id="education">
        <div class="sec-hdr"><div class="sec-ttl">Education</div></div>
        <div id="edu-cards"></div>
      </div>

      <div class="cv-footer" id="cv-footer"></div>
    </div>
  </div>
</div>

<!-- ══ PDF ══════════════════════════════════════════════════════════════════ -->
<div id="pdf-page">
  <div class="pdf-header">
    <div>
      <div class="pdf-name" id="pdf-name"></div>
      <div class="pdf-role" id="pdf-role"></div>
      <div class="pdf-contacts" id="pdf-contacts"></div>
    </div>
    <div class="pdf-qr" id="pdf-qr"></div>
  </div>
  <div class="pdf-divider"></div>
  <div class="pdf-stats" id="pdf-stats"></div>
  <div class="pdf-sec-ttl">Career Summary</div>
  <div class="pdf-lede" id="pdf-lede"></div>
  <div class="pdf-body" id="pdf-body"></div>
  <div class="pdf-sec-ttl">Key Skills</div>
  <div class="pdf-chips" id="pdf-skills"></div>
  <div class="pdf-sec-ttl">Experience</div>
  <div id="pdf-exp"></div>
  <div class="p-note">This CV is a summary. For full task details including challenges, inputs and outputs &mdash; scan the QR code at the top of this page to explore the interactive version.</div>
  <div class="pdf-sp-panel" id="pdf-sp-panel"></div>
  <div class="pdf-sec-ttl">Education</div>
  <div id="pdf-edu"></div>
  <div class="pdf-footer" id="pdf-footer"></div>
</div>

<script>
let CV = null;

function esc(s){ return s ? String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') : ''; }

function chips(list, cls='chip'){
  return list.map(s=>`<span class="${cls}">${esc(s)}</span>`).join('');
}

// ── helpers ───────────────────────────────────────────────────────────────────
function fmtTenureHtml(years, months, numCls){
  let html = '';
  if(years) html += `${years}<span>yr</span> `;
  if(months) html += `${months}<span>mo</span>`;
  if(!years && !months) html += `&lt;1<span>mo</span>`;
  return `<div class=”${numCls}”>${html.trim()}</div>`;
}

// ── Sidebar ───────────────────────────────────────────────────────────────────
function renderSidebar(d){
  const p = d.person;
  document.getElementById('sb-avatar').textContent = p.initials;
  document.getElementById('sb-name').textContent = p.full_name;
  document.getElementById('sb-role').textContent = d.experience.find(e=>e.is_current)?.roles.find(r=>r.is_current)?.title || '';

  const motto = p.motto;
  document.getElementById('sb-motto').innerHTML = motto
    ? `<span class=”sb-motto-quote”>&ldquo;</span>${esc(motto)}<span class=”sb-motto-quote”>&rdquo;</span>` : '';

  const loc = [p.home_city, p.home_country].filter(Boolean).join(', ');
  const relocHtml = (p.relocation && p.country !== 'Australia')
    ? `<div class=”reloc”>${svgIcon('plane',10)} Open to relocation &rarr; ${esc(p.relocation)}</div>`
    : '';
  document.getElementById('sb-location').innerHTML = `
    <div class=”sb-row”>${svgIcon('map-pin',14)}<div class=”sb-txt”>${esc(loc)}${relocHtml}</div></div>`;

  let contacts = '';
  if(p.phone) contacts += `<div class=”sb-row”>${svgIcon('phone',14)}<span class=”sb-txt”>${esc(p.phone)}</span></div>`;
  if(p.phone2) contacts += `<div class=”sb-row”>${svgIcon('phone',14)}<span class=”sb-txt”>${esc(p.phone2)}</span></div>`;
  if(p.email) contacts += `<div class=”sb-row”>${svgIcon('mail',14)}<span class=”sb-txt”>${esc(p.email)}</span></div>`;
  if(p.linkedin) contacts += `<div class=”sb-row”>${svgIcon('linkedin',14)}<span class=”sb-txt”><a href=”${esc(p.linkedin)}” target=”_blank”>LinkedIn</a></span></div>`;
  if(p.github) contacts += `<div class=”sb-row”>${svgIcon('github',14)}<span class=”sb-txt”><a href=”${esc(p.github)}” target=”_blank”>GitHub</a></span></div>`;
  document.getElementById('sb-contacts').innerHTML = contacts;

  const s = d.stats;
  document.getElementById('stat-grid').innerHTML = [
    [s.total_exp, 'Yrs experience'],
    [s.industries, 'Industries'],
    [s.analytics_years, 'Yrs analytics'],
    [s.consulting_years, 'Yrs consulting'],
  ].map(([n,l])=>`<div class=”stat-box”><div class=”stat-val”>${n}</div><div class=”stat-lbl”>${l}</div></div>`).join('');

  document.getElementById('key-skills-wrap').innerHTML =
    d.key_skills.map(s=>`<span class=”sk-pill”>${esc(s)}</span>`).join('');
}

// ── Career Summary ────────────────────────────────────────────────────────────
function renderSummary(d){
  document.getElementById('sum-lede').textContent = d.person.career_summary_lede;
  document.getElementById('sum-body').textContent = d.person.career_summary_body;
}

// ── Summary card CSS fix (applied at render time via class)
// .summary-card already has border-left:3px solid var(--cobalt); border-radius:0 14px 14px 0

// ── Radar ─────────────────────────────────────────────────────────────────────
function drawRadar(){
  const radarAxes = [
    {label:['Data &','Analytics'],  score: CV.radar_axes[0].score},
    {label:['Visuali-','sation'],   score: CV.radar_axes[1].score},
    {label:['AI/ML/','Coding'],     score: CV.radar_axes[2].score},
    {label:['CX'],                  score: CV.radar_axes[3].score},
    {label:['CRM'],                 score: CV.radar_axes[4].score},
    {label:['Team/','Project'],     score: CV.radar_axes[5].score},
  ];
  const RADAR_MAX = CV.radar_max;
  const N=6, RINGS=4, cobalt='#3765F6';
  const canvas=document.getElementById('radarChart');
  const inner=canvas.parentElement;
  const W=inner.clientWidth, H=inner.clientHeight;
  canvas.width=W; canvas.height=H;
  const CX=W/2, CY=H/2;
  const FONT=8, LINE_H=10, GAP=4;
  const R=Math.min(CX,CY)-(GAP+2*LINE_H+6);
  const ctx=canvas.getContext('2d');
  function angle(i){return(Math.PI*2/N)*i-Math.PI/2;}
  function polar(r,i){return{x:CX+r*Math.cos(angle(i)),y:CY+r*Math.sin(angle(i))};}
  function hexPath(r){ctx.beginPath();for(let i=0;i<N;i++){const p=polar(r,i);i===0?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y);}ctx.closePath();}
  for(let ring=1;ring<=RINGS;ring++){hexPath((R/RINGS)*ring);ctx.strokeStyle=cobalt;ctx.lineWidth=0.5;ctx.globalAlpha=0.2;ctx.stroke();ctx.globalAlpha=1;}
  for(let i=0;i<N;i++){const p=polar(R,i);ctx.beginPath();ctx.moveTo(CX,CY);ctx.lineTo(p.x,p.y);ctx.strokeStyle=cobalt;ctx.lineWidth=0.75;ctx.globalAlpha=0.2;ctx.stroke();ctx.globalAlpha=1;}
  hexPath(R);ctx.strokeStyle=cobalt;ctx.lineWidth=2.5;ctx.globalAlpha=0.85;ctx.stroke();ctx.globalAlpha=1;
  ctx.beginPath();
  for(let i=0;i<N;i++){const r=(radarAxes[i].score/RADAR_MAX)*R,p=polar(r,i);i===0?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y);}
  ctx.closePath();ctx.fillStyle='rgba(112,252,142,0.75)';ctx.fill();
  ctx.font=`600 ${FONT}px "Geist Mono"`;ctx.fillStyle=cobalt;ctx.globalAlpha=0.9;
  for(let i=0;i<N;i++){
    const a=angle(i),lx=CX+(R+GAP)*Math.cos(a),ly=CY+(R+GAP)*Math.sin(a);
    const lines=radarAxes[i].label,totalH=lines.length*LINE_H;
    let align=lx<CX-6?'right':lx>CX+6?'left':'center';
    let startY=i===0?ly-totalH-2:i===N/2?ly+4:ly-totalH/2;
    startY=Math.max(2,Math.min(startY,H-totalH-2));
    ctx.textAlign=align;ctx.textBaseline='top';
    lines.forEach((line,li)=>ctx.fillText(line,lx,startY+li*LINE_H));
  }
  ctx.globalAlpha=1;
}

// ── Treemap ───────────────────────────────────────────────────────────────────
function renderTreemap(d){
  const tmSkills = d.treemap_top5;
  if(!tmSkills || !tmSkills.length) return;
  const tmTotal = tmSkills.reduce((s,x)=>s+x.tasks,0);
  let row1=[],row2=[],acc=0;
  for(const s of tmSkills){
    if(acc+s.tasks<=tmTotal*0.55){row1.push(s);acc+=s.tasks;}
    else{row2.push(s);}
  }
  function makeTmRow(items){
    const row=document.createElement('div'); row.className='tm-row';
    items.forEach(s=>{
      const cell=document.createElement('div'); cell.className='tm-cell';
      cell.style.flex=`${s.tasks} 1 0`;
      cell.innerHTML=`
        <div class="tm-top">
          <div class="tm-name">${esc(s.name)}</div>
          <div class="tm-sub">applied in</div>
        </div>
        <div class="tm-bottom">
          <div class="tm-count">${s.tasks}</div>
          <div class="tm-lbl">key tasks</div>
        </div>`;
      row.appendChild(cell);
    });
    return row;
  }
  const tm=document.getElementById('treemapChart');
  tm.innerHTML='';
  tm.appendChild(makeTmRow(row1));
  if(row2.length) tm.appendChild(makeTmRow(row2));
}

// ── Experience ────────────────────────────────────────────────────────────────
function renderExperience(d){
  const container = document.getElementById('exp-cards');
  container.innerHTML = d.experience.map((co, ci) => {
    if(co.roles.length === 1) return singleRoleCard(co, co.roles[0], ci);
    return multiRoleCard(co, ci);
  }).join('');
}

function singleRoleCard(co, role, ci){
  const locStr = co.locations.map(esc).join(', ');
  return `<div class="card">
    <div class="card-head">
      <div>
        <div class="card-title">${esc(role.title)}</div>
        <div class="card-company">${esc(co.company)}</div>
        <div class="card-meta">${esc(co.industry)}${locStr ? ' &middot; ' + locStr : ''}</div>
      </div>
      <div class="tenure-block">
        ${co.is_current ? '<span class="badge-current">CURRENT</span>' : ''}
        ${fmtTenureHtml(role.tenure_years, role.tenure_months, 'tenure-num')}
        <div class="tenure-dates">${role.date_from} &ndash; ${role.date_to}</div>
      </div>
    </div>
    ${role.skills.length ? `<div class="chip-row">${chips(role.skills)}</div>` : ''}
    ${taskList(role.tasks, 'dp_' + ci + '_0')}
  </div>`;
}

function multiRoleCard(co, ci){
  const tmap = {};
  co.transitions.forEach(t => { tmap[t.from_w_id + '|' + t.to_w_id] = t.label; });

  const sortedRoles = [...co.roles].sort((a,b) => {
    if(a.is_current !== b.is_current) return a.is_current ? -1 : 1;
    return 0;
  });

  const rolesHtml = sortedRoles.map((role, ri) => {
    const isCurr = role.is_current;
    let promoHtml = '';
    if(ri > 0){
      const prev = sortedRoles[ri-1];
      const label = tmap[prev.w_id + '|' + role.w_id] || tmap[role.w_id + '|' + prev.w_id] || '&#8593; Promoted';
      const arrow = label.includes('Promot') ? '&#8593;' : '&#8635;';
      promoHtml = `<div class="promo-row"><div class="promo-line"></div><span class="promo-badge">${arrow} ${esc(label.replace(/^[↑⟳]\s*/,''))}</span><div class="promo-line"></div></div>`;
    }
    return `
      ${promoHtml}
      <div class="role-block ${isCurr ? 'current' : ''}">
        <div class="role-head">
          <div class="role-title">${esc(role.title)}</div>
          <div style="text-align:right;flex-shrink:0">
            ${isCurr ? '<span class="badge-current">CURRENT</span>' : ''}
            ${fmtTenureHtml(role.tenure_years, role.tenure_months, 'role-tenure-num')}
            <div class="role-dates">${role.date_from} &ndash; ${role.date_to}</div>
          </div>
        </div>
        ${role.skills.length ? `<div class="chip-row">${chips(role.skills)}</div>` : ''}
        ${taskList(role.tasks, 'dp_' + ci + '_' + ri)}
      </div>`;
  }).join('');

  const locStr = co.locations.map(esc).join(', ');
  return `<div class="company-card">
    <div class="company-head">
      <div>
        <div class="company-name">${esc(co.company)}</div>
        <div class="company-meta">${esc(co.industry)}${locStr ? ' &middot; ' + locStr : ''}</div>
      </div>
      <div style="text-align:right;flex-shrink:0">
        ${co.is_current ? '<span class="badge-current">CURRENT</span>' : ''}
        ${fmtTenureHtml(co.total_tenure_years, co.total_tenure_months, 'tenure-num')}
        <div class="tenure-dates">${co.date_from} &ndash; ${co.date_to}</div>
      </div>
    </div>
    ${rolesHtml}
  </div>`;
}

function taskList(tasks, prefix){
  if(!tasks.length) return '';
  const first3 = tasks.slice(0, 3);
  const rest = tasks.slice(3);
  const smId = prefix + '_sm';

  let html = `<div class="tasks-lbl">Key Tasks</div>`;
  const renderTask = (t, ti, hidden) => {
    const hasDetail = !!(t.description || t.challenge || t.input || t.output || t.outcome);
    const dpId = prefix + '_t' + ti;
    return `<div class="task-row" ${hidden ? 'style="display:none"' : ''} data-extra="${hidden}">
      <div class="task-line">
        <span class="tdot"></span>
        <span class="ttxt">${esc(t.name)}</span>
        ${hasDetail ? `<button class="vd-btn" id="btn_${dpId}" onclick="toggleDetail(this,'${dpId}')">View details &#8595;</button>` : ''}
      </div>
      <div class="dp" id="${dpId}">
        <div class="dp-inner">
          ${t.skills.length ? `<div class="task-chip-row">${chips(t.skills,'task-chip')}</div>` : ''}
          ${t.description ? `<div class="task-desc">${esc(t.description)}</div>` : ''}
          ${(t.challenge||t.input||t.output||t.outcome) ? `<div class="detail-grid">
            ${t.challenge ? `<div class="dpc"><div class="dpl">${svgIcon('help-circle',11)}Challenge</div><div class="dpv">${esc(t.challenge)}</div></div>` : ''}
            ${t.input ? `<div class="dpc"><div class="dpl">${svgIcon('database',11)}Input</div><div class="dpv">${esc(t.input)}</div></div>` : ''}
            ${t.output ? `<div class="dpc"><div class="dpl">${svgIcon('chart-bar',11)}Output</div><div class="dpv">${esc(t.output)}</div></div>` : ''}
            ${t.outcome ? `<div class="dpc oc"><div class="dpl">${svgIcon('trending-up',11)}Outcome</div><div class="dpv">${esc(t.outcome)}</div></div>` : ''}
          </div>` : ''}
        </div>
      </div>
    </div>`;
  };

  html += first3.map((t,ti) => renderTask(t, ti, false)).join('');

  if(rest.length){
    html += `<div class="extra-tasks" id="${smId}">${rest.map((t,ti) => renderTask(t, ti+3, false)).join('')}</div>`;
    html += `<div class="show-more-row"><button class="show-more-btn" id="smb_${smId}" onclick="toggleShowMore('${smId}',this,${rest.length})">${svgIcon('chevron-down',10)} Show ${rest.length} more task${rest.length>1?'s':''}</button></div>`;
  }
  return html;
}

// ── Side Projects ─────────────────────────────────────────────────────────────
function renderProjects(d){
  document.getElementById('proj-cards').innerHTML = d.side_projects.map((sp, i) => {
    const dpId = 'proj_dp_' + i;
    const hasDetail = sp.challenge || sp.built_with || sp.output;
    const year = sp.completed_on ? sp.completed_on.split(/[-/ ]/)[0] : '';
    return `<div class="proj-card">
      <div class="proj-head">
        <div class="proj-title">${esc(sp.name)}</div>
        <div class="proj-btns">
          ${sp.url ? `<a class="proj-link" href="${esc(sp.url)}" target="_blank">${svgIcon('external-link',10)} View project</a>` : ''}
          ${hasDetail ? `<button class="proj-link" id="btn_${dpId}" onclick="toggleProjDetail('${dpId}',this)">${svgIcon('chevron-down',10)} View details</button>` : ''}
        </div>
      </div>
      <div class="proj-meta">Side project${year ? ' &middot; ' + esc(year) : ''}</div>
      ${sp.description ? `<div class="proj-desc">${esc(sp.description)}</div>` : ''}
      ${sp.skills.length ? `<div class="chip-row">${chips(sp.skills)}</div>` : ''}
      <div class="proj-dp" id="${dpId}">
        <div class="proj-dp-inner">
          <div class="proj-detail-grid">
            ${sp.challenge ? `<div class="dpc"><div class="dpl">Challenge</div><div class="dpv">${esc(sp.challenge)}</div></div>` : ''}
            ${sp.built_with ? `<div class="dpc"><div class="dpl">Built with</div><div class="dpv">${esc(sp.built_with)}</div></div>` : ''}
            ${sp.output ? `<div class="dpc" style="grid-column:1"><div class="dpl">Output</div><div class="dpv">${esc(sp.output)}</div></div>` : ''}
          </div>
        </div>
      </div>
    </div>`;
  }).join('');
}

// ── Education ─────────────────────────────────────────────────────────────────
function renderEducation(d){
  document.getElementById('edu-cards').innerHTML = d.education.map(e => {
    const isCert = (e.type || '').toLowerCase().includes('cert');
    if(isCert){
      const skillHtml = e.skill ? `<div class="edu-pills"><span class="edu-pill">${esc(e.skill)}</span></div>` : '';
      return `<div class="edu-card">
        <div class="edu-head">
          <div class="edu-left">
            <div class="edu-deg">${esc(e.degree)}</div>
            <div class="edu-inst">${esc(e.institute)}</div>
          </div>
          <div style="display:flex;flex-direction:column;align-items:flex-end;gap:5px">
            <span class="cert-badge">Certified</span>
            ${e.to_year ? `<div class="edu-yr-range">${esc(e.to_year)}</div>` : ''}
          </div>
        </div>
        <div class="edu-div"></div>
        ${skillHtml}
      </div>`;
    }
    const lvlLabel = (e.type||'').toLowerCase().includes('post') ? 'Postgraduate' : 'Undergraduate';
    const majorsHtml = e.majors.length
      ? `<div class="mjr-row"><span class="mjr-lbl">Major(s)</span>${e.majors.map(m=>`<span class="mjr-chip">${esc(m)}</span>`).join('')}</div>` : '';
    const locStr = [e.city, e.country].filter(Boolean).join(', ');
    return `<div class="edu-card">
      <div class="edu-head">
        <div class="edu-left">
          <div class="edu-deg">${esc(e.degree)}</div>
          <div class="edu-inst">${esc(e.institute)}</div>
          <div class="edu-meta">
            <span class="edu-lvl">${lvlLabel}</span>
            ${locStr ? `<span class="edu-loc">${svgIcon('map-pin-sm',11)} ${esc(locStr)}</span>` : ''}
          </div>
          ${majorsHtml}
        </div>
        <div>
          ${e.grad_year ? `<div class="edu-yr">${esc(e.grad_year)}</div>` : ''}
          ${(e.from_year||e.to_year) ? `<div class="edu-yr-range">${esc(e.from_year)} &ndash; ${esc(e.to_year)}</div>` : ''}
        </div>
      </div>
    </div>`;
  }).join('');
}

// ── PDF ───────────────────────────────────────────────────────────────────────
function renderPDF(d){
  const p = d.person;
  const currentRole = d.experience.find(e=>e.is_current)?.roles.find(r=>r.is_current);
  document.getElementById('pdf-name').textContent = p.full_name;
  document.getElementById('pdf-role').textContent = currentRole?.title || '';
  const contacts = [p.phone, p.email, p.linkedin, p.github].filter(Boolean);
  document.getElementById('pdf-contacts').innerHTML = contacts.map(c=>`<span class="pdf-contact">${esc(c)}</span>`).join('');
  document.getElementById('pdf-qr').innerHTML = d.qr_b64 ? `<img src="data:image/png;base64,${d.qr_b64}" alt="QR">` : '';

  const s = d.stats;
  document.getElementById('pdf-stats').innerHTML = [
    [s.total_exp, 'Yrs experience'],
    [s.industries, 'Industries'],
    [s.analytics_years, 'Yrs analytics'],
    [s.consulting_years, 'Yrs consulting'],
  ].map(([n,l])=>`<div class="pdf-stat"><div class="pdf-stat-num">${n}</div><div class="pdf-stat-lbl">${l}</div></div>`).join('');

  document.getElementById('pdf-lede').textContent = p.career_summary_lede;
  document.getElementById('pdf-body').textContent = p.career_summary_body;
  document.getElementById('pdf-skills').innerHTML = d.key_skills.map(s=>`<span class="pdf-chip">${esc(s)}</span>`).join('');

  document.getElementById('pdf-exp').innerHTML = d.experience.flatMap(co =>
    co.roles.map(role => `<div class="pdf-role-block">
      <div class="pdf-role-top">
        <div>
          <div class="pdf-role-title">${esc(role.title)}</div>
          <div class="pdf-co">${esc(co.company)}</div>
          <div class="pdf-meta">${[co.industry, co.locations.join(', '), role.date_from + ' – ' + role.date_to].filter(Boolean).join(' · ')}</div>
        </div>
        <div class="pdf-role-tenure">${role.tenure_str}</div>
      </div>
      <ul class="p-tlist">${role.tasks.map(t=>`<li><div class="p-tname">${esc(t.name)}</div></li>`).join('')}</ul>
    </div>`)
  ).join('');

  const spPanel = document.getElementById('pdf-sp-panel');
  spPanel.innerHTML = `<div class="pdf-sp-text">
    <strong>There's more beyond this page.</strong> I work on a number of side projects that sit outside a traditional CV format — ${esc(d.sp_teaser)}. Scan the QR code to explore them in full on the interactive version of this CV.
  </div>
  <div class="pdf-qr2">${d.qr_b64 ? `<img src="data:image/png;base64,${d.qr_b64}" alt="QR">` : ''}</div>`;

  document.getElementById('pdf-edu').innerHTML = d.education.map(e => {
    const isCert = (e.type||'').toLowerCase().includes('cert');
    return `<div class="pdf-edu-entry">
      <div class="pdf-edu-top">
        <div>
          <div class="pdf-edu-inst">${esc(e.institute)} ${isCert ? `<span class="pdf-badge-cert">Certified</span>` : ''}</div>
          <div class="pdf-edu-deg">${esc(e.degree)}${e.majors.length ? ' — ' + e.majors.map(esc).join(', ') : ''}</div>
        </div>
        <div class="pdf-edu-deg">${e.grad_year || e.to_year}</div>
      </div>
    </div>`;
  }).join('');

  document.getElementById('pdf-footer').innerHTML =
    `<span>${esc(p.full_name)}</span><span>Last updated ${esc(d.generated_date)} &middot; sourced from Google Sheets &middot; powered by Python &amp; DuckDB</span>`;

  document.getElementById('cv-footer').innerHTML =
    `Last updated ${esc(d.generated_date)} &middot; sourced from Google Sheets &middot; powered by Python &amp; DuckDB`;
}

// ── Interactions ──────────────────────────────────────────────────────────────
function collapseSection(sectionId, btn){
  const section = document.getElementById(sectionId);
  const isCollapsing = btn.textContent.includes('Collapse');
  section.querySelectorAll('.dp, .proj-dp').forEach(dp => {
    dp.style.maxHeight = isCollapsing ? '0' : dp.scrollHeight + 'px';
    dp.style.opacity = isCollapsing ? '0' : '1';
    if(!isCollapsing){
      const b = document.getElementById('btn_' + dp.id);
      if(b){ b.textContent = 'Hide details &#8593;'; b.classList.add('open'); }
    } else {
      const b = document.getElementById('btn_' + dp.id);
      if(b){ b.innerHTML = 'View details &#8595;'; b.classList.remove('open'); }
    }
  });
  section.querySelectorAll('.extra-tasks').forEach(et => {
    et.style.maxHeight = isCollapsing ? '0' : et.scrollHeight + 'px';
    et.style.opacity = isCollapsing ? '0' : '1';
  });
  btn.innerHTML = isCollapsing ? '&#9656; Expand all' : '&#9662; Collapse all';
}

function toggleDetail(btn, id){
  const dp = document.getElementById(id);
  const isOpen = dp.style.maxHeight && dp.style.maxHeight !== '0px';
  if(isOpen){
    dp.style.maxHeight = '0'; dp.style.opacity = '0';
    btn.innerHTML = 'View details &#8595;'; btn.classList.remove('open');
  } else {
    dp.style.maxHeight = dp.scrollHeight + 'px'; dp.style.opacity = '1';
    btn.innerHTML = 'Hide details &#8593;'; btn.classList.add('open');
  }
}

function toggleProjDetail(id, btn){
  const dp = document.getElementById(id);
  const isOpen = dp.style.maxHeight && dp.style.maxHeight !== '0px';
  if(isOpen){
    dp.style.maxHeight = '0'; dp.style.opacity = '0';
    btn.innerHTML = svgIcon('chevron-down',10) + ' View details';
  } else {
    dp.style.maxHeight = dp.scrollHeight + 'px'; dp.style.opacity = '1';
    btn.innerHTML = svgIcon('chevron-up',10) + ' Hide details';
  }
}

function toggleShowMore(id, btn, n){
  const c = document.getElementById(id);
  const isOpen = c.style.maxHeight && c.style.maxHeight !== '0px';
  if(isOpen){
    c.style.maxHeight = '0'; c.style.opacity = '0';
    btn.innerHTML = svgIcon('chevron-down',10) + ` Show ${n} more task${n>1?'s':''}`;
  } else {
    c.style.maxHeight = c.scrollHeight + 'px'; c.style.opacity = '1';
    btn.innerHTML = svgIcon('chevron-up',10) + ' Show less';
  }
}

// ── Scroll spy ────────────────────────────────────────────────────────────────
function navTo(sectionId, el){
  document.getElementById(sectionId)?.scrollIntoView({behavior:'smooth'});
  document.querySelectorAll('.nav-item').forEach(i=>i.classList.remove('active'));
  el.classList.add('active');
}

function initScrollSpy(){
  const map = {
    'summary':'nav-summary','skills-overview':'nav-skills',
    'experience':'nav-exp','side-projects':'nav-proj','education':'nav-edu'
  };
  const obs = new IntersectionObserver(entries => {
    entries.forEach(e => {
      const navId = map[e.target.id];
      if(navId) document.getElementById(navId)?.classList.toggle('active', e.isIntersecting);
    });
  }, {threshold: 0.3});
  Object.keys(map).forEach(id => { const el = document.getElementById(id); if(el) obs.observe(el); });
}

// ── Print ─────────────────────────────────────────────────────────────────────
function showPDF(){ window.print(); }

// ── Icons ─────────────────────────────────────────────────────────────────────
function svgIcon(name, size=14){
  const icons = {
    'map-pin': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>`,
    'phone': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.69 13a19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 3.6 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.91 9.91a16 16 0 0 0 6.16 6.16l.91-.91a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/></svg>`,
    'mail': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>`,
    'linkedin': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="currentColor"><path d="M16 8a6 6 0 0 1 6 6v7h-4v-7a2 2 0 0 0-2-2 2 2 0 0 0-2 2v7h-4v-7a6 6 0 0 1 6-6z"/><rect x="2" y="9" width="4" height="12"/><circle cx="4" cy="4" r="2"/></svg>`,
    'github': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/></svg>`,
    'external-link': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>`,
    'plane': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>`,
    'user': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`,
    'briefcase': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/></svg>`,
    'code': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`,
    'school': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>`,
    'download': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`,
    'chevron-down': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>`,
    'chevron-up': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg>`,
    'chevron-right': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>`,
    'help-circle': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
    'database': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>`,
    'chart-bar': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>`,
    'trending-up': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>`,
    'map-pin-sm': `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>`,
  };
  return icons[name] || '';
}

// ── Boot ──────────────────────────────────────────────────────────────────────
fetch('cv_data.json')
  .then(r => r.json())
  .then(data => {
    CV = data;
    renderSidebar(data);
    renderSummary(data);
    renderExperience(data);
    renderProjects(data);
    renderEducation(data);
    renderPDF(data);
    document.fonts.ready.then(() => drawRadar());
    renderTreemap(data);
    initScrollSpy();
  })
  .catch(err => {
    document.body.innerHTML = `<div style="padding:2rem;font-family:monospace">
      <h2>Could not load cv_data.json</h2>
      <p>Serve this directory via a local HTTP server (e.g. <code>python -m http.server 8000</code> in the docs/ folder) then open <a href="http://localhost:8000">http://localhost:8000</a>.</p>
      <pre>${err}</pre>
    </div>`;
  });
</script>
</body>
</html>"""

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== CV Build starting ===")

    print("\n[1/7] Authenticating with Google Sheets...")
    client = authenticate()
    print("      OK")

    print("\n[2/7] Loading sheets...")
    tabs = load_sheets(client)

    print("\n[3/7] Generating QR code...")
    qr_b64 = make_qr()
    print("      OK")

    print("\n[4/7] Running transformations...")
    cv_data = build_cv_data(tabs)
    cv_data["qr_b64"] = qr_b64
    print("      OK")

    print("\n[5/7] Backing up existing index.html...")
    idx = DOCS / "index.html"
    if idx.exists():
        shutil.copy(idx, DOCS / "index.previous.html")
        print("      Backed up to docs/index.previous.html")
    else:
        print("      No existing file to back up")

    print("\n[6/7] Writing output files...")
    (SHARED / "cv_data.json").write_text(json.dumps(cv_data, indent=2, default=str), encoding="utf-8")
    (DOCS / "cv_data.json").write_text(json.dumps(cv_data, indent=2, default=str), encoding="utf-8")
    (DOCS / "index.html").write_text(HTML_TEMPLATE, encoding="utf-8")
    print("      shared/cv_data.json OK")
    print("      docs/cv_data.json OK")
    print("      docs/index.html OK")

    print("\n[7/7] Summary:")
    p = cv_data["person"]
    print(f"      Name:        {p['full_name']}")
    print(f"      Experience:  {len(cv_data['experience'])} companies")
    print(f"      Side projects: {len(cv_data['side_projects'])}")
    print(f"      Education:   {len(cv_data['education'])} entries")
    print(f"      Stats:       {cv_data['stats']}")
    print(f"      Generated:   {cv_data['generated_date']}")

    print("\n=== Build complete. ===")


if __name__ == "__main__":
    main()
