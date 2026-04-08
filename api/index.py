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

def claude_request(prompt, max_tokens=2000):
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=55) as resp:
        result = json.loads(resp.read())
    text = result["content"][0]["text"].strip()
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text

def extract_criteria(doc_text, keywords):
    prompt = f"""Extract recruiting search criteria from this content.

Document:
{doc_text}

Recruiter notes:
{keywords}

Return ONLY JSON:
{{"job_title":"","keywords":[],"comp_min":0,"comp_max":0,"location":"","summary":""}}

keywords: include titles, skills, tools, platforms, industries. comp in thousands (85k=85). summary: 2 sentences on ideal candidate."""
    return json.loads(claude_request(prompt, 1000))

def batch_score(candidates, criteria_text):
    """Score up to 50 candidates in one Claude call."""
    lines = []
    for i, c in enumerate(candidates[:50]):
        notes = c.get("notes","")[:400]
        lines.append(f"{i}|{c.get('name','')}|{c.get('current_company','')}|{c.get('former_companies','')}|{c.get('tags_str','')}|{c.get('location','')}|{notes}")

    prompt = f"""You are a recruiting assistant. Find candidates matching the role criteria below.

CRITERIA:
{criteria_text}

CANDIDATES (index|name|company|former|tags|location|notes):
{"~".join(lines)}

Return ONLY a JSON array of matches with score >= 55. Max 25 results, sorted by score descending.
[{{"index":0,"score":85,"snippet":"why they fit in one sentence","comp_found":"$X from notes or empty"}}]

Return ONLY the JSON array."""
    text = claude_request(prompt, 3000)
    return json.loads(text)

def fetch_candidates(stages, tag_filter, special_filter, role_filter, last_contact_months):
    filters = []
    if stages:
        sf = [{"property": "Stage", "status": {"equals": s}} for s in stages]
        filters.append(sf[0] if len(sf) == 1 else {"or": sf})
    if tag_filter:
        filters.append({"property": "Tag", "multi_select": {"contains": tag_filter}})
    if special_filter:
        filters.append({"property": "Special", "multi_select": {"contains": special_filter}})
    if role_filter:
        filters.append({"property": "Role", "multi_select": {"contains": role_filter}})
    if last_contact_months:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(last_contact_months)*30)).isoformat()
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
            def loc(k): return ", ".join(i.get("name","") for i in props.get(k,{}).get("multi_select",[]))
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
            })
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if len(candidates) >= 2000:
            break
    return candidates

def filter_comp(candidates, comp_min, comp_max):
    if not comp_min and not comp_max:
        return candidates
    filtered = []
    for c in candidates:
        matches = re.findall(r'\$?(\d{2,3})k', c.get("notes","").lower())
        if not matches:
            filtered.append(c)
            continue
        vals = [int(x) for x in matches]
        avg = sum(vals)/len(vals)
        if comp_min and comp_max:
            if avg >= int(comp_min)*0.8 and avg <= int(comp_max)*1.3:
                filtered.append(c)
        elif comp_min:
            if avg >= int(comp_min)*0.8: filtered.append(c)
        elif comp_max:
            if avg <= int(comp_max)*1.3: filtered.append(c)
    return filtered

def filter_location(candidates, location):
    if not location:
        return candidates
    loc_lower = location.lower()
    return [c for c in candidates if loc_lower in c.get("location","").lower() or loc_lower in c.get("notes","").lower() or "remote" in loc_lower]

def tag_candidate(page_id, tag_name, existing_roles):
    new_roles = list(set(existing_roles + [tag_name]))
    notion_request("PATCH", f"/pages/{page_id}", {
        "properties": {"Role": {"multi_select": [{"name": r} for r in new_roles]}}
    })

def parse_multipart(content_type, body):
    fs = cgi.FieldStorage(fp=io.BytesIO(body), environ={
        'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': content_type, 'CONTENT_LENGTH': str(len(body))
    })
    result = {}
    for key in ['keywords','comp_min','comp_max','location','tag_filter','special_filter','role_filter','last_contact_months','stages','mode','tag_name']:
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
                ct = self.headers.get('Content-Type','')
                cl = int(self.headers.get('Content-Length',0))
                body = self.rfile.read(cl)
                form = parse_multipart(ct, body)

                keywords = form.get('keywords','').strip()
                comp_min = form.get('comp_min','').strip()
                comp_max = form.get('comp_max','').strip()
                location = form.get('location','').strip()
                tag_filter = form.get('tag_filter','').strip()
                special_filter = form.get('special_filter','').strip()
                role_filter = form.get('role_filter','').strip()
                last_contact = form.get('last_contact_months','').strip()
                stages = json.loads(form.get('stages','[]'))
                mode = form.get('mode','view')
                tag_name = form.get('tag_name','').strip()
                file_bytes = form.get('file_bytes', b'')
                file_filename = form.get('file_filename','')

                # Extract document text
                doc_text = ""
                if file_bytes:
                    fn = file_filename.lower()
                    if fn.endswith('.pdf'):
                        doc_text = extract_text_from_pdf(file_bytes)
                    elif fn.endswith('.docx') or fn.endswith('.doc'):
                        doc_text = extract_text_from_docx(file_bytes)

                # Build criteria
                criteria_text = ""
                use_ai = bool(doc_text or keywords)
                if use_ai:
                    ai = extract_criteria(doc_text, keywords)
                    criteria_text = f"Role: {ai.get('job_title','')}. Keywords: {', '.join(ai.get('keywords',[]))}. {ai.get('summary','')} Comp: ${ai.get('comp_min',comp_min)}k-${ai.get('comp_max',comp_max)}k. Location: {ai.get('location','') or location}."
                    if not comp_min and ai.get('comp_min'): comp_min = str(ai['comp_min'])
                    if not comp_max and ai.get('comp_max'): comp_max = str(ai['comp_max'])
                    if not location and ai.get('location'): location = ai['location']

                # Fetch and filter
                candidates = fetch_candidates(stages, tag_filter, special_filter, role_filter, last_contact)
                if location:
                    candidates = filter_location(candidates, location)
                if comp_min or comp_max:
                    candidates = filter_comp(candidates, comp_min, comp_max)

                results = []
                if use_ai and candidates:
                    # Score in batches of 50
                    all_scored = []
                    for i in range(0, min(len(candidates), 150), 50):
                        batch = candidates[i:i+50]
                        try:
                            scored = batch_score(batch, criteria_text)
                            for s in scored:
                                idx = s.get('index', 0)
                                if 0 <= idx < len(batch):
                                    c = batch[idx]
                                    all_scored.append({
                                        "id": c["id"], "name": c["name"],
                                        "current_company": c["current_company"],
                                        "location": c["location"], "stage": c["stage"],
                                        "comp": s.get('comp_found',''), "tags": c["tags"],
                                        "snippet": s.get('snippet',''),
                                        "score": s.get('score',0),
                                        "existing_roles": c["roles"]
                                    })
                        except Exception:
                            pass
                    all_scored.sort(key=lambda x: x.get('score',0), reverse=True)
                    results = all_scored[:30]
                else:
                    results = [{
                        "id": c["id"], "name": c["name"],
                        "current_company": c["current_company"],
                        "location": c["location"], "stage": c["stage"],
                        "comp": "", "tags": c["tags"], "snippet": "",
                        "existing_roles": c["roles"]
                    } for c in candidates]

                # Tag in Notion if requested
                tagged_count = 0
                if mode == 'tag' and tag_name:
                    for r in results:
                        try:
                            tag_candidate(r["id"], tag_name, r.get("existing_roles",[]))
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
        if self.path.split('?')[0] in ('/', '/index.html'):
            self._serve_file(os.path.join(os.path.dirname(__file__),'..','public','index.html'), 'text/html')
        else:
            self.send_error(404)

    def _serve_file(self, filepath, ct):
        try:
            with open(filepath,'rb') as f: content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except: self.send_error(404)

    def _ok(self, data): self._json(200, data)
    def _err(self, code, msg): self._json(code, {'error': msg})

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Access-Control-Allow-Origin','*')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args): pass
