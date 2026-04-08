from http.server import BaseHTTPRequestHandler
import json
import os
import re
import cgi
import io
import tempfile
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "036bdd7a61694c0e95450a26984e84c4")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

def notion_request(method, path, payload=None):
    url = f"https://api.notion.com/v1{path}"
    headers = {"Authorization": f"Bearer {NOTION_API_KEY}", "Content-Type": "application/json", "Notion-Version": "2022-06-28"}
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def extract_text_from_pdf(file_bytes):
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
        f.write(file_bytes)
        tmp = f.name
    try:
        result = subprocess.run(
            ['python3', '-c', f'from pdfminer.high_level import extract_text; print(extract_text("{tmp}"))'],
            capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip()[:8000]
    finally:
        try: os.unlink(tmp)
        except: pass

def extract_text_from_docx(file_bytes):
    with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as f:
        f.write(file_bytes)
        tmp = f.name
    try:
        result = subprocess.run(
            ['python3', '-c', f'from docx import Document; doc = Document("{tmp}"); print("\\n".join(p.text for p in doc.paragraphs))'],
            capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip()[:8000]
    finally:
        try: os.unlink(tmp)
        except: pass

def extract_criteria_with_claude(document_text, keywords):
    """Use Claude to extract search criteria from uploaded document."""
    prompt = f"""You are a recruiting assistant. Extract search criteria from the following document and/or notes.

Document:
{document_text}

Additional notes from recruiter:
{keywords}

Return ONLY a JSON object with these fields (use empty string if not found):
{{
  "job_title": "primary job title",
  "keywords": ["skill1", "skill2", "tool1", ...],
  "comp_min": 0,
  "comp_max": 0,
  "location": "",
  "experience_summary": "2-3 sentence summary of what kind of person they're looking for"
}}

For keywords, include: job titles, skills, tools, platforms, industries, experience types. Include variations.
For comp, extract numbers in thousands (e.g. $85k = 85).
Return ONLY the JSON, no markdown."""

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": prompt}]
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
        method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    text = result["content"][0]["text"].strip()
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return json.loads(text)

def score_candidate_with_claude(candidate, criteria_text):
    """Use Claude to score a candidate against criteria."""
    notes = candidate.get("notes", "")[:2000]
    prompt = f"""You are a recruiting assistant. Score this candidate's fit for the role.

ROLE CRITERIA:
{criteria_text}

CANDIDATE:
Name: {candidate.get('name', '')}
Current Company: {candidate.get('current_company', '')}
Former Companies: {candidate.get('former_companies', '')}
Tags: {candidate.get('tags', '')}
Location: {candidate.get('location', '')}
Notes: {notes}

Return ONLY a JSON object:
{{
  "score": 0-100,
  "match": true or false,
  "snippet": "one sentence explaining why they match or don't",
  "comp_found": "compensation found in notes or empty string"
}}

Score 70+ means strong match. Score 50-69 means possible match. Below 50 means weak match.
Only return match: true if score >= 55.
Return ONLY the JSON."""

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}]
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
        method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    text = result["content"][0]["text"].strip()
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return json.loads(text)

def fetch_all_candidates(stages, tag_filter, special_filter, role_filter, last_contact_months):
    """Fetch candidates from Notion with basic filters."""
    filters = []

    # Stage filter
    if stages:
        stage_filters = [{"property": "Stage", "status": {"equals": s}} for s in stages]
        if len(stage_filters) == 1:
            filters.append(stage_filters[0])
        else:
            filters.append({"or": stage_filters})

    # Tag filter
    if tag_filter:
        filters.append({"property": "Tag", "multi_select": {"contains": tag_filter}})

    # Special filter
    if special_filter:
        filters.append({"property": "Special", "multi_select": {"contains": special_filter}})

    # Role filter
    if role_filter:
        filters.append({"property": "Role", "multi_select": {"contains": role_filter}})

    # Last contact filter
    if last_contact_months:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(last_contact_months) * 30)).isoformat()
        filters.append({"or": [
            {"property": "Last Catchup", "date": {"before": cutoff}},
            {"property": "Last Catchup", "date": {"is_empty": True}}
        ]})

    payload = {"page_size": 100}
    if len(filters) == 1:
        payload["filter"] = filters[0]
    elif len(filters) > 1:
        payload["filter"] = {"and": filters}

    candidates = []
    cursor = None

    while True:
        if cursor:
            payload["start_cursor"] = cursor
        data = notion_request("POST", f"/databases/{NOTION_DATABASE_ID}/query", payload)

        for page in data.get("results", []):
            props = page.get("properties", {})

            def rt(k): return "".join(t.get("plain_text","") for t in props.get(k,{}).get("rich_text",[]))
            def ms(k): return ", ".join(i.get("name","") for i in props.get(k,{}).get("multi_select",[]))
            def ms_list(k): return [i.get("name","") for i in props.get(k,{}).get("multi_select",[])]
            def status(k):
                s = props.get(k,{}).get("status"); return s.get("name","") if s else ""
            def loc(k):
                items = props.get(k,{}).get("multi_select",[])
                return ", ".join(i.get("name","") for i in items)

            name_prop = props.get("\ufeffName") or props.get("Name", {})
            name = "".join(t.get("plain_text","") for t in name_prop.get("title",[])).strip()
            if not name:
                continue

            candidates.append({
                "id": page["id"],
                "name": name,
                "current_company": rt("Current Company"),
                "former_companies": rt("Former Companies"),
                "notes": "".join(t.get("plain_text","") for t in props.get("Notes",{}).get("rich_text",[])),
                "stage": status("Stage"),
                "tags": ms_list("Tag"),
                "tags_str": ms("Tag"),
                "specials": ms_list("Special"),
                "roles": ms_list("Role"),
                "location": loc("Current Location"),
                "phone": props.get("Phone",{}).get("phone_number","") or "",
                "email": props.get("Primary Email",{}).get("email","") or "",
            })

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        # Safety limit — max 500 candidates per scrub
        if len(candidates) >= 500:
            break

    return candidates

def filter_by_comp(candidates, comp_min, comp_max):
    """Filter candidates by comp range found in their notes."""
    if not comp_min and not comp_max:
        return candidates
    
    filtered = []
    for c in candidates:
        notes = c.get("notes", "").lower()
        # Find comp mentions in notes
        comp_matches = re.findall(r'\$?(\d{2,3})k', notes)
        if not comp_matches:
            filtered.append(c)  # Include if no comp info
            continue
        comp_values = [int(x) for x in comp_matches]
        avg_comp = sum(comp_values) / len(comp_values)
        if comp_min and comp_max:
            if avg_comp >= int(comp_min) * 0.8 and avg_comp <= int(comp_max) * 1.3:
                filtered.append(c)
        elif comp_min:
            if avg_comp >= int(comp_min) * 0.8:
                filtered.append(c)
        elif comp_max:
            if avg_comp <= int(comp_max) * 1.3:
                filtered.append(c)
    return filtered

def filter_by_location(candidates, location):
    """Filter candidates by location."""
    if not location:
        return candidates
    loc_lower = location.lower()
    filtered = []
    for c in candidates:
        cand_loc = c.get("location", "").lower()
        notes_loc = c.get("notes", "").lower()
        if loc_lower in cand_loc or loc_lower in notes_loc or "remote" in loc_lower:
            filtered.append(c)
    return filtered

def add_role_tag_to_candidate(page_id, tag_name, existing_roles):
    """Add a role tag to a candidate in Notion without removing existing ones."""
    new_roles = list(set(existing_roles + [tag_name]))
    notion_request("PATCH", f"/pages/{page_id}", {
        "properties": {
            "Role": {"multi_select": [{"name": r} for r in new_roles]}
        }
    })

def parse_multipart(content_type, body):
    fs = cgi.FieldStorage(fp=io.BytesIO(body), environ={
        'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': content_type, 'CONTENT_LENGTH': str(len(body))
    })
    result = {}
    text_fields = ['keywords', 'comp_min', 'comp_max', 'location', 'tag_filter',
                   'special_filter', 'role_filter', 'last_contact_months', 'stages', 'mode', 'tag_name']
    for key in text_fields:
        if key in fs:
            result[key] = fs[key].value
    if 'file' in fs:
        item = fs['file']
        result['file_filename'] = item.filename or ''
        result['file_bytes'] = item.file.read()
    return result

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.split('?')[0] == '/api/scrub':
            try:
                ct = self.headers.get('Content-Type', '')
                cl = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(cl)
                form = parse_multipart(ct, body)

                keywords = form.get('keywords', '').strip()
                comp_min = form.get('comp_min', '').strip()
                comp_max = form.get('comp_max', '').strip()
                location = form.get('location', '').strip()
                tag_filter = form.get('tag_filter', '').strip()
                special_filter = form.get('special_filter', '').strip()
                role_filter = form.get('role_filter', '').strip()
                last_contact = form.get('last_contact_months', '').strip()
                stages_json = form.get('stages', '["Intro Interviewed","Connection"]')
                mode = form.get('mode', 'view')
                tag_name = form.get('tag_name', '').strip()
                file_bytes = form.get('file_bytes', b'')
                file_filename = form.get('file_filename', '')

                stages = json.loads(stages_json) if stages_json else ["Intro Interviewed", "Connection"]

                # Extract document text
                doc_text = ""
                if file_bytes:
                    fn = file_filename.lower()
                    if fn.endswith('.pdf'):
                        doc_text = extract_text_from_pdf(file_bytes)
                    elif fn.endswith('.docx') or fn.endswith('.doc'):
                        doc_text = extract_text_from_docx(file_bytes)

                # Build criteria text
                criteria_text = ""
                ai_criteria = None
                if doc_text or keywords:
                    ai_criteria = extract_criteria_with_claude(doc_text, keywords)
                    criteria_text = f"""
Job Title: {ai_criteria.get('job_title', '')}
Keywords: {', '.join(ai_criteria.get('keywords', []))}
Experience: {ai_criteria.get('experience_summary', '')}
Location: {ai_criteria.get('location', '') or location}
Comp: ${ai_criteria.get('comp_min', comp_min)}k - ${ai_criteria.get('comp_max', comp_max)}k
"""
                    # Use AI-extracted comp if not manually set
                    if not comp_min and ai_criteria.get('comp_min'):
                        comp_min = str(ai_criteria['comp_min'])
                    if not comp_max and ai_criteria.get('comp_max'):
                        comp_max = str(ai_criteria['comp_max'])
                    if not location and ai_criteria.get('location'):
                        location = ai_criteria['location']
                else:
                    criteria_text = f"Looking for candidates. Location: {location}. Comp: ${comp_min}k-${comp_max}k."

                # Fetch candidates from Notion
                candidates = fetch_all_candidates(stages, tag_filter, special_filter, role_filter, last_contact)

                # Apply location filter
                if location:
                    candidates = filter_by_location(candidates, location)

                # Apply comp filter
                if comp_min or comp_max:
                    candidates = filter_by_comp(candidates, comp_min, comp_max)

                # If we have AI criteria, score candidates
                results = []
                if criteria_text and (doc_text or keywords):
                    # Batch score — limit to 100 for performance
                    to_score = candidates[:100]
                    for c in to_score:
                        try:
                            score_result = score_candidate_with_claude(c, criteria_text)
                            if score_result.get('match'):
                                results.append({
                                    "id": c["id"],
                                    "name": c["name"],
                                    "current_company": c["current_company"],
                                    "location": c["location"],
                                    "stage": c["stage"],
                                    "comp": score_result.get('comp_found', ''),
                                    "tags": c["tags"],
                                    "snippet": score_result.get('snippet', ''),
                                    "score": score_result.get('score', 0),
                                    "existing_roles": c["roles"]
                                })
                        except Exception:
                            pass
                    # Sort by score
                    results.sort(key=lambda x: x.get('score', 0), reverse=True)
                else:
                    # No AI scoring — return all filtered candidates
                    for c in candidates[:50]:
                        results.append({
                            "id": c["id"],
                            "name": c["name"],
                            "current_company": c["current_company"],
                            "location": c["location"],
                            "stage": c["stage"],
                            "comp": "",
                            "tags": c["tags"],
                            "snippet": "",
                            "existing_roles": c["roles"]
                        })

                # Tag results in Notion if mode = tag
                tagged_count = 0
                if mode == 'tag' and tag_name:
                    for r in results:
                        try:
                            add_role_tag_to_candidate(r["id"], tag_name, r.get("existing_roles", []))
                            r["tagged"] = True
                            tagged_count += 1
                        except Exception:
                            r["tagged"] = False

                self._ok({"results": results, "tagged_count": tagged_count})

            except urllib.error.HTTPError as e:
                self._err(500, f'API error: {e.read().decode()[:200]}')
            except Exception as e:
                self._err(500, str(e))
        else:
            self.send_error(404)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/', '/index.html'):
            self._serve_file(os.path.join(os.path.dirname(__file__), '..', 'public', 'index.html'), 'text/html')
        else:
            self.send_error(404)

    def _serve_file(self, filepath, content_type):
        try:
            with open(filepath, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404)

    def _ok(self, data):
        self._json(200, data)

    def _err(self, code, msg):
        self._json(code, {'error': msg})

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args): pass
