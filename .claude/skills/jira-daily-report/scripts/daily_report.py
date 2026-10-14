#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["matplotlib>=3.8"]
# ///
"""Rapport du daily Jira : burnup du sprint (JPG), mouvements depuis hier et état du sprint (Markdown).

Jira n'exporte pas le burnup : on rejoue l'appel interne du front
(/rest/greenhopper/1.0/rapid/charts/scopechangeburndownchart), qui renvoie le
journal des événements du sprint, puis on recalcule les courbes comme Jira.

Configuration par variables d'environnement :
  JIRA_BASE_URL         ex. https://votre-site.atlassian.net
  JIRA_EMAIL, JIRA_API_TOKEN   jeton API classique, en Basic auth
  JIRA_BOARD_ID         board suivi (ou --board)
  JIRA_BOARD_SECTIONS   rubriques de l'état du sprint, en JSON (facultatif, voir board_sections)
"""
import argparse
import base64
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

COLORS = {
    "scope": "#e2483d",
    "done": "#82b536",
    "guide": "#8c8f97",
    "today": "#b4b6bc",
    "nonworking": "#f4f5f6",
    "grid": "#dddedd",
    "text": "#6b6e76",
    "title": "#172b4d",
}
FR_MONTHS = "janv. févr. mars avr. mai juin juil. août sept. oct. nov. déc.".split()


# --- Récupération -------------------------------------------------------------

def jira_get(base_url, path, params=None):
    email, token = os.environ.get("JIRA_EMAIL"), os.environ.get("JIRA_API_TOKEN")
    if not email or not token:
        sys.exit("JIRA_EMAIL et JIRA_API_TOKEN doivent être définis.")
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"Jira HTTP {e.code} sur {path} : {e.read().decode(errors='replace')[:300]}")


def fetch_bundle(base_url, board_id, sprint_id=None, estimate=None):
    if sprint_id is None:
        active = jira_get(base_url, f"/rest/agile/1.0/board/{board_id}/sprint", {"state": "active"})["values"]
        if len(active) != 1:
            found = ", ".join(f"{s['id']} ({s['name']})" for s in active) or "aucun"
            sys.exit(f"Sprint actif ambigu sur le board {board_id} : {found}. Préciser --sprint.")
        sprint = active[0]
    else:
        sprint = jira_get(base_url, f"/rest/agile/1.0/sprint/{sprint_id}")

    config = jira_get(base_url, f"/rest/agile/1.0/board/{board_id}/configuration")
    if estimate is None:
        estimation = config.get("estimation", {})
        if estimation.get("type") != "field":
            sys.exit(f"Estimation du board non gérée ({estimation}). Préciser --estimate.")
        estimate = "field_" + estimation["field"]["fieldId"]

    chart = jira_get(
        base_url,
        "/rest/greenhopper/1.0/rapid/charts/scopechangeburndownchart",
        {"rapidViewId": board_id, "sprintId": sprint["id"], "statisticFieldId": estimate},
    )
    columns = [{"name": c["name"], "statusIds": [s["id"] for s in c["statuses"]]}
               for c in config["columnConfig"]["columns"]]
    issues = fetch_sprint_issues(base_url, sprint["id"], estimate.removeprefix("field_"))
    return {"baseUrl": base_url, "boardId": board_id, "sprint": sprint, "chart": chart,
            "columns": columns, "issues": issues}


# Merge requests GitLab, pull requests GitHub / Bitbucket.
MR_URL = re.compile(r"https?://[^\s<>\"'()\[\]]+/(?:-/merge_requests|pull|pull-requests)/\d+")


def adf_text(node):
    """Texte d'un document ADF (commentaire Jira), URLs des liens et des cartes comprises."""
    if isinstance(node, list):
        return " ".join(adf_text(child) for child in node)
    if not isinstance(node, dict):
        return ""
    parts = [node.get("text") or "", (node.get("attrs") or {}).get("url") or ""]
    parts += [(m.get("attrs") or {}).get("href") or "" for m in node.get("marks") or []]
    return " ".join(parts + [adf_text(node.get("content") or [])])


def fetch_sprint_issues(base_url, sprint_id, estimate_field):
    """Tickets du sprint (sous-tâches comprises), réduits à ce que le rapport utilise."""
    issues, page_token = [], None
    while True:
        params = {"jql": f"sprint = {sprint_id}", "maxResults": 100,
                  "fields": f"summary,status,assignee,issuetype,parent,comment,{estimate_field}"}
        if page_token:
            params["nextPageToken"] = page_token
        page = jira_get(base_url, "/rest/api/3/search/jql", params)
        for raw in page["issues"]:
            comments = raw["fields"]["comment"]
            if comments["total"] > len(comments["comments"]):
                comments = jira_get(base_url, f"/rest/api/3/issue/{raw['key']}/comment", {"maxResults": 5000})
            issues.append(slim_issue(raw, estimate_field, comments["comments"]))
        page_token = page.get("nextPageToken")
        if not page_token:
            return issues


def slim_issue(raw, estimate_field, comments):
    fields = raw["fields"]
    parent = fields.get("parent")
    merge_requests = []
    # Les MR viennent des commentaires du connecteur GitLab (« mentioned this issue in a merge request »)
    # ou collés à la main ; leur état (ouverte/fusionnée) n'est pas connu côté Jira.
    for comment in comments:
        for url in MR_URL.findall(adf_text(comment["body"])):
            if url not in merge_requests:
                merge_requests.append(url)
    return {
        "key": raw["key"],
        "summary": fields["summary"].strip(),
        "statusId": fields["status"]["id"],
        "subtask": fields["issuetype"].get("subtask", False),
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "estimate": fields.get(estimate_field),
        "parent": parent and {
            "key": parent["key"],
            "summary": parent["fields"]["summary"].strip(),
            "isEpic": parent["fields"]["issuetype"].get("hierarchyLevel", 0) >= 1,
        },
        "mergeRequests": merge_requests,
    }


# --- Rejeu des événements -------------------------------------------------------

DAY = 86_400_000


def to_dt(ms):
    # Les timestamps greenhopper sont l'heure locale du board encodée comme de l'UTC.
    return datetime.fromtimestamp(ms / 1000, timezone.utc).replace(tzinfo=None)


def from_local(text):
    """« 2026-10-13 09:00 » (heure locale du board) -> timestamp greenhopper."""
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def sprint_events(chart):
    subtasks = chart.get("issueToParentKeys") or {}
    for ts, events in sorted((int(t), evs) for t, evs in chart["changes"].items()):
        for ev in events:
            if ev["key"] not in subtasks:  # l'estimation est portée par le parent
                yield ts, ev


def apply_event(issues, ev):
    """Met à jour l'état [dans le sprint, estimation, terminé] du ticket ; renvoie (était dans le sprint, état)."""
    state = issues.setdefault(ev["key"], [False, None, False])
    was_in = state[0]
    if "added" in ev:
        state[0] = ev["added"]
    if "statC" in ev:
        state[1] = ev["statC"].get("newValue")
    if "column" in ev:
        # Calé sur le rendu Jira : seul notDone=true vaut « non terminé ».
        state[2] = ev["column"].get("notDone") is not True
    return was_in, state


def totals(issues):
    scope = sum(est or 0 for inside, est, _ in issues.values() if inside)
    done = sum(est or 0 for inside, est, is_done in issues.values() if inside and is_done)
    return scope, done


def snapshot(chart, until):
    issues = {}
    for ts, ev in sprint_events(chart):
        if ts > until:
            break
        apply_event(issues, ev)
    return issues


def work_rates(chart):
    return (chart.get("workRateData") or {}).get("rates") or [
        {"start": chart["startTime"], "end": chart["endTime"], "rate": 1}
    ]


# --- Courbes du burnup ----------------------------------------------------------

def compute(chart):
    start, end, now = chart["startTime"], chart["endTime"], chart["now"]
    last = max(start, min(chart.get("completeTime") or now, end))

    issues = {}
    scope_pts, done_pts = [], []
    started = False
    for ts, ev in sprint_events(chart):
        if ts > last:
            break
        if ts >= start and not started:
            started = True
            s, d = totals(issues)
            scope_pts.append((start, s))
            done_pts.append((start, d))
        was_in, state = apply_event(issues, ev)
        if not started:
            continue
        s, d = totals(issues)
        if ("added" in ev or "statC" in ev) and (was_in or state[0]):
            scope_pts.append((ts, s))
        if d != done_pts[-1][1] or ("column" in ev and state[0] and state[2]):
            done_pts.append((ts, d))

    s, d = totals(issues)
    if not started:
        scope_pts.append((start, s))
        done_pts.append((start, d))
    scope_pts.append((last, s))
    done_pts.append((last, d))

    # Directive : de 0 au périmètre actuel, à plat sur les jours non travaillés.
    rates = work_rates(chart)

    def worked(t):
        return sum(max(0, min(r["end"], t) - r["start"]) * r["rate"] for r in rates)

    total = worked(end) or 1
    guide = [(rates[0]["start"], 0.0)] + [(r["end"], s * worked(r["end"]) / total) for r in rates]

    return {
        "start": start,
        "end": end,
        "now": now,
        "last": last,
        "scope": scope_pts,
        "done": done_pts,
        "guide": guide,
        "nonworking": [(r["start"], r["end"]) for r in rates if r["rate"] == 0],
        "scope_now": s,
        "done_now": d,
        "guide_now": s * worked(last) / total,
    }


# --- Mouvements depuis hier -----------------------------------------------------

def previous_working_day(chart, t):
    """Même heure le jour ouvré précédent (lundi -> vendredi), d'après le calendrier du board."""
    rates = work_rates(chart)

    def working(ts):
        for r in rates:
            if r["start"] <= ts < r["end"]:
                return r["rate"] > 0
        return to_dt(ts).weekday() < 5  # hors sprint, le calendrier est inconnu

    t -= DAY
    while not working(t):
        t -= DAY
    return t


def changes_since(chart, since, until):
    """Diff de l'état des tickets entre `since` et `until` (un ticket ajouté puis estimé n'apparaît qu'une fois)."""
    before, after = snapshot(chart, since), snapshot(chart, until)
    summaries = chart.get("issueToSummary") or {}
    scope_before, done_before = totals(before)
    scope_after, done_after = totals(after)
    result = {
        "since": f"{to_dt(since):%Y-%m-%d %H:%M}",
        "doneDelta": done_after - done_before,
        "scopeDelta": scope_after - scope_before,
        "done": [], "reopened": [], "added": [], "removed": [], "reestimated": [],
    }

    blank = [False, None, False]
    for key in sorted(set(before) | set(after), key=issue_order):
        b_in, b_est, b_done = before.get(key, blank)
        a_in, a_est, a_done = after.get(key, blank)
        item = {"key": key, "summary": summaries.get(key, "").strip(), "estimate": a_est}
        if a_in and a_done and not b_done:
            result["done"].append(item)
        if a_in and b_in and b_done and not a_done:
            result["reopened"].append(item)
        if a_in and not b_in:
            result["added"].append(item)
        if b_in and not a_in:
            result["removed"].append({**item, "estimate": b_est})
        if a_in and b_in and a_est != b_est:
            result["reestimated"].append({**item, "previous": b_est})
    return result


def issue_order(key):
    project, number = key.rsplit("-", 1)
    return project, int(number)


# --- État du board --------------------------------------------------------------

LAYOUTS = {
    "topic": "nombre de tickets par epic",
    "review": "par assigné, avec les MR/PR trouvées dans les commentaires",
    "assignee": "par assigné",
    "todo": "nombre par assigné, liste des tickets non affectés",
}


def board_sections(columns):
    """Rubriques de l'état du sprint, dans l'ordre du rapport : [(colonne, titre, présentation)].

    JIRA_BOARD_SECTIONS, si défini : liste JSON de {"column", "title" (facultatif), "layout"}, ex.
    [{"column": "EN REVIEW", "title": "Review à faire", "layout": "review"}, {"column": "À FAIRE", "layout": "todo"}].
    Sinon : toutes les colonnes sauf la dernière (terminé), de droite à gauche, la première en « todo ».
    """
    raw = os.environ.get("JIRA_BOARD_SECTIONS")
    if not raw:
        names = [c["name"] for c in columns[:-1]]
        return [(name, name, "todo" if i == 0 else "assignee") for i, name in reversed(list(enumerate(names)))]
    sections = []
    for entry in json.loads(raw):
        layout = entry.get("layout", "assignee")
        if layout not in LAYOUTS:
            sys.exit(f"JIRA_BOARD_SECTIONS : présentation « {layout} » inconnue ({', '.join(LAYOUTS)}).")
        sections.append((entry["column"], entry.get("title", entry["column"]), layout))
    return sections


def board_state(columns, issues):
    """Tickets des colonnes suivies ; les sous-tâches sont regroupées sous leur parent, par assigné."""
    by_key = {issue["key"]: issue for issue in issues}
    column_of = {sid: c["name"].casefold() for c in columns for sid in c["statusIds"]}

    def topic(issue):
        parent = issue and issue["parent"]
        if not parent:
            return None
        return parent["summary"] if parent["isEpic"] else topic(by_key.get(parent["key"]))

    state = []
    for column, title, layout in board_sections(columns):
        if column.casefold() not in column_of.values():
            print(f"Colonne « {column} » absente du board : rubrique ignorée.", file=sys.stderr)
            continue
        members = [issue for issue in issues if column_of.get(issue["statusId"]) == column.casefold()]
        lines = {}
        for issue in members:
            grouped = issue["subtask"] and issue["parent"]
            key = issue["parent"]["key"] if grouped else issue["key"]
            line = lines.setdefault((key, issue["assignee"]), {
                "key": key, "summary": "", "assignee": issue["assignee"], "estimate": None, "topic": None,
                "tickets": 0, "subtasks": [], "mergeRequests": [],
            })
            if grouped:
                line["subtasks"].append(issue["key"])
                line["summary"] = line["summary"] or issue["parent"]["summary"]
            else:
                line.update(summary=issue["summary"], estimate=issue["estimate"])
            line["tickets"] += 1
            line["topic"] = line["topic"] or topic(issue)
            line["mergeRequests"] += [mr for mr in issue["mergeRequests"] if mr not in line["mergeRequests"]]
        state.append({
            "column": column, "title": title, "layout": layout, "tickets": len(members),
            "estimate": sum(issue["estimate"] or 0 for issue in members),
            "lines": sorted(lines.values(), key=lambda line: issue_order(line["key"])),
        })
    return state


# --- Rendu ----------------------------------------------------------------------

def fmt(value):
    return f"{value:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def clean_name(name):
    # La police par défaut de matplotlib n'a pas les emojis des noms de sprint.
    return re.sub(r"\s+", " ", re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F\u200D]", "", name)).strip()


def render(model, sprint_name, stat_name, out, figsize=(12, 6.4), dpi=150, image_format="jpg"):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.ticker import MaxNLocator

    def x(ms):
        return mdates.date2num(to_dt(ms))

    start, end, now, last = model["start"], model["end"], model["now"], model["last"]
    scope_now, done_now = model["scope_now"], model["done_now"]

    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    # Marges en pouces : l'en-tête (titre, sous-titre, légende) garde sa place quel que soit le format.
    width_in, height_in = figsize
    left = 0.84 / width_in
    fig.subplots_adjust(left=left, right=1 - 0.6 / width_in, top=1 - 1.55 / height_in, bottom=0.6 / height_in)

    for a, b in model["nonworking"]:
        ax.axvspan(x(a), x(b), color=COLORS["nonworking"], lw=0, zorder=0)

    gx, gy = zip(*model["guide"])
    ax.plot([x(t) for t in gx], gy, color=COLORS["guide"], lw=2, zorder=2)

    marker_style = {"mfc": "white", "mew": 1.5, "ms": 5}
    for series, color, marker in (("scope", COLORS["scope"], "s"), ("done", COLORS["done"], "o")):
        pts = model[series]
        xs, ys = [x(t) for t, _ in pts], [v for _, v in pts]
        # Pas de marqueur sur les points synthétiques (début de sprint, maintenant).
        ax.step(xs, ys, where="post", color=color, lw=2, zorder=4,
                marker=marker, markevery=list(range(1, len(pts) - 1)), mec=color, **marker_style)

    if last < end:
        ax.plot([x(last), x(end)], [scope_now, scope_now], color=COLORS["scope"], lw=2,
                ls=(0, (0.5, 2.5)), dash_capstyle="round", zorder=3)

    if start <= now <= end:
        ax.axvline(x(now), color=COLORS["today"], lw=1, zorder=1)
        ax.annotate("Aujourd'hui", xy=(x(now), 1), xycoords=("data", "axes fraction"), xytext=(0, 4),
                    textcoords="offset points", ha="center", va="bottom", fontsize=8,
                    fontweight="bold", color=COLORS["text"])

    # Valeurs actuelles en texte : le vert seul est peu lisible sur fond blanc.
    for value, dy in ((scope_now, 6), (done_now, 6)):
        ax.annotate(f"{fmt(value)}", xy=(x(last), value), xytext=(6, dy), textcoords="offset points",
                    ha="left", va="bottom", fontsize=9, fontweight="bold", color=COLORS["title"], zorder=6)

    ax.set_xlim(x(start), x(end))
    ax.set_ylim(0, max([scope_now] + [v for _, v in model["scope"]]) * 1.05 or 1)

    day = 86_400_000
    step = max(1, round((end - start) / day / 10))
    ticks = list(range(start, end - day, step * day))  # comme Jira : pas d'étiquette collée au bord droit
    ax.set_xticks([x(t) for t in ticks])
    ax.set_xticklabels([f"{FR_MONTHS[to_dt(t).month - 1]} {to_dt(t).day}" for t in ticks], ha="left")
    ax.yaxis.set_major_locator(MaxNLocator(nbins=8, integer=True))

    ax.grid(axis="y", color=COLORS["grid"], lw=1)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(COLORS["grid"])
    ax.tick_params(colors=COLORS["text"], labelsize=8.5, length=0, pad=6)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontweight("bold")
    ax.set_ylabel(stat_name.upper(), color=COLORS["text"], fontsize=8.5, fontweight="bold", labelpad=8)

    handles = [
        Line2D([], [], color=COLORS["scope"], lw=2, marker="s", mec=COLORS["scope"], **marker_style),
        Line2D([], [], color=COLORS["scope"], lw=2, ls=(0, (2, 1.5))),
        Line2D([], [], color=COLORS["done"], lw=2, marker="o", mec=COLORS["done"], **marker_style),
        Line2D([], [], color=COLORS["guide"], lw=2),
    ]
    labels = ["Portée du travail", "Projection du périmètre", "Travail terminé", "Directive"]
    ax.legend(handles, labels, loc="lower left", bbox_to_anchor=(0, 1.07), ncol=4, frameon=False,
              fontsize=9.5, labelcolor=COLORS["title"], handlelength=2.2, columnspacing=2.5)

    pct = round(100 * done_now / scope_now) if scope_now else 0
    fig.text(left, 1 - 0.3 / height_in, f"Burnup · {sprint_name}", fontsize=15, fontweight="bold", color=COLORS["title"])
    fig.text(left, 1 - 0.55 / height_in,
             f"{fmt(done_now)} / {fmt(scope_now)} {stat_name} terminés ({pct} %) · "
             f"directive à date : {fmt(model['guide_now'])} · données du {to_dt(now):%d/%m/%Y %H:%M}",
             fontsize=9.5, color=COLORS["text"])

    options = {"pil_kwargs": {"quality": 90}} if image_format == "jpg" else {}
    fig.savefig(out, format=image_format, facecolor="white", **options)
    plt.close(fig)


FR_DAYS = "lun. mar. mer. jeu. ven. sam. dim.".split()


def plural(count, word):
    return f"{count} {word}{'s' if count > 1 else ''}"


def mr_link(url):
    project, kind, number = re.search(r"/([^/]+)/(-/merge_requests|pull|pull-requests)/(\d+)", url).groups()
    return f"[{project}{'!' if kind == '-/merge_requests' else '#'}{number}]({url})"


def by_assignee(lines):
    """Lignes groupées par assigné : les plus chargés d'abord, « Non affecté » en dernier."""
    groups = {}
    for line in lines:
        groups.setdefault(line["assignee"], []).append(line)
    return sorted(groups.items(), key=lambda g: (g[0] is None, -sum(line["tickets"] for line in g[1]), g[0] or ""))


def board_markdown(state, link, amount):
    def describe(line):
        parts = [f"{link(line['key'])} {line['summary']}"]
        if line["estimate"] is not None:
            parts.append(amount(line["estimate"]))
        if line["subtasks"]:
            parts.append(plural(len(line["subtasks"]), "sous-tâche"))
        return " · ".join(parts)

    def merge_requests(line):
        if not line["mergeRequests"]:
            return "⚠️ aucune MR/PR dans les commentaires"
        # Numéro décroissant : les plus récentes d'abord (l'état ouverte/fusionnée n'est pas connu).
        mrs = sorted(line["mergeRequests"], key=lambda url: -int(url.rsplit("/", 1)[1]))
        more = f" (+{len(mrs) - 3} plus anciennes)" if len(mrs) > 3 else ""
        return "MR/PR " + ", ".join(mr_link(url) for url in mrs[:3]) + more

    def tickets_of(lines):
        return sum(line["tickets"] for line in lines)

    out = ["", "## 🗂️ État du sprint"]
    for section in state:
        header = f"### {section['title']} · {plural(section['tickets'], 'ticket')}"
        if section["estimate"]:
            header += f", {amount(section['estimate'])}"
        out += ["", header]
        lines = section["lines"]
        if not lines:
            out.append("Aucun ticket.")
        elif section["layout"] == "topic":
            topics = {}
            for line in lines:
                topics.setdefault(line["topic"] or "Sans epic", []).append(line)
            for topic, group in sorted(topics.items(), key=lambda t: -tickets_of(t[1])):
                out.append(f"- {topic} : {tickets_of(group)} · " + ", ".join(link(line["key"]) for line in group))
        elif section["layout"] == "todo":
            assigned = [line for line in lines if line["assignee"]]
            out.append(" · ".join(f"{name} : {tickets_of(group)}" for name, group in by_assignee(assigned)))
            unassigned = [line for line in lines if not line["assignee"]]
            if unassigned:
                out += ["", f"**Non affectés · {plural(tickets_of(unassigned), 'ticket')}**"]
                out += [f"- {describe(line)} · {line['topic'] or 'Sans epic'}" for line in unassigned]
            else:
                out.append("")
                out.append("Tous les tickets à faire sont affectés.")
        else:
            for name, group in by_assignee(lines):
                # Ligne vide avant chaque nom, sinon Markdown le colle au dernier ticket de la liste précédente.
                out += ["", f"**{name or 'Non affecté'}** · {plural(tickets_of(group), 'ticket')}"]
                for line in group:
                    detail = f" · {merge_requests(line)}" if section["layout"] == "review" else ""
                    out.append(f"- {describe(line)}{detail}")
    return out


def report_markdown(image_ref, base_url, sprint_name, stat_name, model, changes, state):
    unit = "SP" if stat_name.lower() == "story points" else stat_name

    def amount(value):
        return "non estimé" if value is None else f"{fmt(value)} {unit}"

    def signed(value):
        return ("+" if value > 0 else "") + amount(value)

    def link(key):
        return f"[{key}]({base_url.rstrip('/')}/browse/{key})"

    def line(item, detail):
        return f"- {link(item['key'])} {item['summary']} · {detail}"

    now, since = to_dt(model["now"]), datetime.fromisoformat(changes["since"])
    scope, done = model["scope_now"], model["done_now"]
    pct = round(100 * done / scope) if scope else 0
    lines = [
        f"# 📋 Daily du {now:%d/%m/%Y} · {sprint_name}",
        "",
        f"![Burnup du sprint]({image_ref})",
        "",
        f"**{fmt(done)} / {fmt(scope)} {unit} terminés ({pct} %)** · directive à date : {amount(model['guide_now'])}",
        "",
        f"## 🕘 Depuis hier ({FR_DAYS[since.weekday()]} {since:%d/%m %H:%M})",
        "",
        f"Terminé **{signed(changes['doneDelta'])}** · Périmètre **{signed(changes['scopeDelta'])}**",
    ]
    sections = [
        ("✅ Terminés", changes["done"], lambda i: amount(i["estimate"])),
        ("↩️ Réouverts", changes["reopened"], lambda i: amount(i["estimate"])),
        ("➕ Ajoutés au sprint", changes["added"], lambda i: amount(i["estimate"])),
        ("➖ Retirés du sprint", changes["removed"], lambda i: amount(i["estimate"])),
        ("✏️ Ré-estimés", changes["reestimated"], lambda i: f"{amount(i['previous'])} → {amount(i['estimate'])}"),
    ]
    for title, items, detail in sections:
        if items:
            lines += ["", f"### {title} ({len(items)})"]
            lines += [line(item, detail(item)) for item in items]
    if not any(items for _, items, _ in sections):
        lines += ["", "Aucun mouvement sur le sprint."]
    if state:
        lines += board_markdown(state, link, amount)
    return "\n".join(lines) + "\n"


# --- Teams ----------------------------------------------------------------------

def teams_html(markdown, image_tag=None):
    """Convertit le rapport Markdown en HTML de message Teams.

    Teams ignore les sauts de ligne : chaque ligne finit par <br>, et deux <br> séparent les blocs.
    Sans `image_tag`, la ligne d'image est retirée.
    """
    def inline(text):
        text = html.escape(text, quote=False)
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)

    blocks = []
    for block in markdown.strip().split("\n\n"):
        rendered = []
        for line in block.split("\n"):
            if line.startswith("!["):
                if image_tag:
                    rendered.append(image_tag)
            elif heading := re.match(r"#+ (.*)", line):
                rendered.append(f"<b>{inline(heading[1])}</b>")
            elif line.startswith("- "):
                rendered.append("• " + inline(line[2:]))
            else:
                rendered.append(inline(line))
        if rendered:
            blocks.append("<br>\n".join(rendered))
    return "<br><br>\n".join(blocks)


# Teams refuse les messages de plus de 28 Ko (RequestEntityTooLarge) ; 27 000 octets passent.
TEAMS_MAX_BYTES = 27_000


def embedded_image(png, colors):
    """Balise <img> d'un PNG en data URI, réduit à une palette de `colors` couleurs (net pour un graphique)."""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        palette = image.convert("RGB").quantize(colors=colors, dither=Image.Dither.NONE)
        buffer = io.BytesIO()
        palette.save(buffer, "PNG", optimize=True)
    data = base64.b64encode(buffer.getvalue()).decode()
    return f'<img src="data:image/png;base64,{data}" alt="Burnup du sprint">'


def teams_message(markdown, model, sprint_name, stat_name, embed_image):
    """HTML du message Teams ; avec `embed_image`, le burnup le plus net qui tient sous TEAMS_MAX_BYTES.

    Teams l'affiche en vignette (~180 px de haut, dimensions HTML/CSS ignorées) et en pleine résolution au clic.
    """
    if embed_image:
        # Rendu dédié, plus étroit que le JPG : le texte du graphique y est proportionnellement plus grand.
        for width in (1000, 900, 800, 700, 600):
            png = io.BytesIO()
            render(model, sprint_name, stat_name, png, figsize=(9, 4.8), dpi=width / 9, image_format="png")
            for colors in (32, 16):
                message = teams_html(markdown, embedded_image(png.getvalue(), colors))
                if len(message.encode()) <= TEAMS_MAX_BYTES:
                    return message
        print("Burnup trop lourd pour un message Teams : publié sans image.", file=sys.stderr)
    return teams_html(markdown)


def publish_teams(webhook_url, message, attempts=10):
    """POST {"text": message} sur le webhook (Power Automate) ; réessaie avec un backoff exponentiel."""
    body = json.dumps({"text": message}, ensure_ascii=False).encode()
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(webhook_url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                print(f"Teams : HTTP {resp.status}", file=sys.stderr)
                return
        except urllib.error.HTTPError as e:
            error = f"HTTP {e.code} : {e.read().decode(errors='replace')[:500]}"
        except urllib.error.URLError as e:
            error = str(e.reason)
        delay = min(2 ** attempt, 120)
        print(f"Teams : échec {attempt}/{attempts} ({error})" + (f", nouvel essai dans {delay} s" if attempt < attempts else ""),
              file=sys.stderr)
        if attempt < attempts:
            time.sleep(delay)
    sys.exit("Publication Teams impossible.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--board", type=int, default=os.environ.get("JIRA_BOARD_ID"),
                        help="ID du board (défaut : JIRA_BOARD_ID)")
    parser.add_argument("--sprint", type=int, help="ID du sprint (défaut : sprint actif du board)")
    parser.add_argument("--estimate", help="statisticFieldId, ex. field_customfield_10016 (défaut : celui du board)")
    parser.add_argument("-o", "--out", default="burnup.jpg", help="Fichier JPG produit")
    parser.add_argument("--save-json", help="Sauvegarde les données Jira récupérées")
    parser.add_argument("--from-json", help="Rend depuis une sauvegarde au lieu d'appeler Jira")
    parser.add_argument("--report", help="Rapport Markdown : burnup, mouvements depuis hier, état du sprint")
    parser.add_argument("--since", help="Début de la fenêtre « depuis hier », heure du board, ex. '2026-10-13 09:00' "
                                        "(défaut : même heure, jour ouvré précédent)")
    parser.add_argument("--teams-html", help="Écrit le message Teams (HTML) dans ce fichier")
    parser.add_argument("--embed-image", action="store_true", help="Intègre le burnup au message Teams (data URI)")
    parser.add_argument("--publish", action="store_true", help="Publie le message Teams sur TEAMS_WEBHOOK_URL")
    args = parser.parse_args()
    if args.publish and not os.environ.get("TEAMS_WEBHOOK_URL"):
        sys.exit("--publish : TEAMS_WEBHOOK_URL doit être défini.")

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as f:
            bundle = json.load(f)
    else:
        if not os.environ.get("JIRA_BASE_URL") or args.board is None:
            sys.exit("JIRA_BASE_URL et JIRA_BOARD_ID (ou --board) doivent être définis.")
        bundle = fetch_bundle(os.environ["JIRA_BASE_URL"], args.board, args.sprint, args.estimate)
    base_url = bundle.get("baseUrl") or os.environ.get("JIRA_BASE_URL", "")
    if args.save_json:
        with open(args.save_json, "w", encoding="utf-8") as f:
            json.dump(bundle, f, ensure_ascii=False, indent=1)

    chart = bundle["chart"]
    sprint = bundle.get("sprint") or {}
    sprint_name = sprint.get("name") or f"Sprint {sprint.get('id', '?')}"
    stat_name = (chart.get("statisticField") or {}).get("name", "Estimation")

    model = compute(chart)
    since = from_local(args.since) if args.since else previous_working_day(chart, model["now"])
    changes = changes_since(chart, since, model["last"])
    state = None
    if "issues" in bundle:
        state = board_state(bundle["columns"], bundle["issues"])
    else:
        print("Sauvegarde sans tickets (format antérieur) : état du sprint ignoré.", file=sys.stderr)

    render(model, clean_name(sprint_name), stat_name, args.out)
    image_ref = args.out
    if args.report:
        image_ref = os.path.relpath(os.path.abspath(args.out), os.path.dirname(os.path.abspath(args.report)))
    markdown = report_markdown(image_ref, base_url, sprint_name, stat_name, model, changes, state)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(markdown)
    if args.teams_html or args.publish:
        message = teams_message(markdown, model, clean_name(sprint_name), stat_name, args.embed_image)
        if args.teams_html:
            with open(args.teams_html, "w", encoding="utf-8") as f:
                f.write(message)
        if args.publish:
            publish_teams(os.environ["TEAMS_WEBHOOK_URL"], message)
    print(json.dumps({
        "sprint": sprint_name,
        "statistic": stat_name,
        "done": model["done_now"],
        "scope": model["scope_now"],
        "guideline": round(model["guide_now"], 1),
        "dataTime": f"{to_dt(model['now']):%Y-%m-%d %H:%M}",
        "image": args.out,
        "report": args.report,
        "teamsHtml": args.teams_html,
        "published": args.publish,
        "sinceYesterday": changes,
        "board": state,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
