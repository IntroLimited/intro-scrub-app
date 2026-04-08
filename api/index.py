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

def extract_keywords(text):
    """Extract meaningful keywords from text for matching."""
    # Remove common stop words and extract meaningful terms
    stop_words = {'the','a','an','and','or','but','in','on','at','to','for','of','with',
                  'is','are','was','were','be','been','have','has','had','do','does','did',
                  'will','would','could','should','may','might','must','shall','can',
                  'this','that','these','those','we','you','they','he','she','it','i',
                  'our','your','their','its','my','his','her','who','which','what','how',
                  'when','where','why','all','each','every','both','few','more','most',
                  'other','some','such','no','nor','not','only','own','same','so','than',
                  'too','very','just','about','above','after','before','between','into',
                  'through','during','including','while','although','because','since',
                  'report','role','position','looking','seeking','candidate','experience',
                  'work','working','team','company','brand','business','years','year'}
    
    words = re.findall(r'\b[a-zA-Z][a-zA-Z0-9+#\-]{2,}\b', text.lower())
    keywords = [w for w in words if w not in stop_words and len(w) > 2]
    
    # Also extract multi-word phrases that are likely meaningful
    phrases = re.findall(r'\b(?:email marketing|lifecycle marketing|ecommerce|e-commerce|social media|content marketing|paid media|growth marketing|brand marketing|digital marketing|product management|supply chain|general manager|creative director|art director|design director|marketing manager|klaviyo|shopify|salesforce|google analytics|facebook ads|instagram|tiktok|linkedin|dtc|b2b|b2c|saas|crm|sms|roi|kpi|ltv|cac|ctr|roas)\b', text.lower())
    
    return list(set(keywords[:50] + phrases))

def score_candidate_keywords(candidate, keywords):
    """Score a candidate based on keyword matches. Fast, no API calls."""
    if not keywords:
        return 0, ""
    
    searchable = " ".join([
        candidate.get("notes", ""),
        candidate.get("current_company", ""),
        candidate.get("former_companies", ""),
        candidate.get("tags_str", ""),
        candidate.get("specials_str", ""),
        candidate.get("location", ""),
    ]).lower()
    
    matched = [kw for kw in keywords if kw.lower() in searchable]
    score = len(matched)
    
    snippet = ""
    if matched:
        # Build a snippet showing which keywords matched
        top_matches = matched[:5]
        snippet = f"Matches: {', '.join(top_matches)}"
    
    return score, snippet

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
                "specials_str": ms("Special"),
                "roles": ms_list("Role"),
                "location": ", ".join(i.get("name","") for i in props.get("Current Location",{}).get("multi_select",[])),
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
    return [c for c in candidates if loc_lower in c.get("location","").lower() or "remote" in loc_lower]

def tag_candidate(page_id, tag_name, existing_roles):
    new_roles = list(set(existing_roles + [tag_name]))
    notion_request("PATCH", f"/pages/{page_id}", {
        "properties": {"Role": {"multi_select": [{"name": r} for r in new_roles]}}
    })

def extract_comp_from_notes(notes):
    """Extract compensation from notes."""
    match = re.search(r'COMPENSATION\s*\n?(.*?)(?:\n\n|\Z)', notes, re.DOTALL | re.IGNORECASE)
    if match:
        comp_text = match.group(1).strip()[:80]
        return comp_text
    # Fallback: look for dollar amounts
    amounts = re.findall(r'\$\d+k(?:\s*-\s*\$?\d+k)?', notes, re.IGNORECASE)
    return amounts[0] if amounts else ""

def parse_multipart(content_type, body):
    fs = cgi.FieldStorage(fp=io.BytesIO(body), environ={
        'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': content_type, 'CONTENT_LENGTH': str(len(body))
    })
    result = {}
    for key in ['keywords','comp_min','comp_max','location','tag_filter','special_filter',
                'role_filter','last_contact_months','stages','mode','tag_name']:
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

                keywords_text = form.get('keywords','').strip()
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

                # Build keyword list from doc + notes
                all_text = " ".join([doc_text, keywords_text])
                keywords = extract_keywords(all_text) if all_text.strip() else []

                # Fetch and filter candidates
                candidates = fetch_candidates(stages, tag_filter, special_filter, role_filter, last_contact)
                if location:
                    candidates = filter_location(candidates, location)
                if comp_min or comp_max:
                    candidates = filter_comp(candidates, comp_min, comp_max)

                # Score by keyword match
                results = []
                if keywords:
                    scored = []
                    for c in candidates:
                        score, snippet = score_candidate_keywords(c, keywords)
                        if score > 0:
                            scored.append({
                                "id": c["id"], "name": c["name"],
                                "current_company": c["current_company"],
                                "location": c["location"], "stage": c["stage"],
                                "comp": extract_comp_from_notes(c["notes"]),
                                "tags": c["tags"], "snippet": snippet,
                                "score": score, "existing_roles": c["roles"]
                            })
                    scored.sort(key=lambda x: x["score"], reverse=True)
                    results = scored[:100]
                else:
                    # No keywords — return all filtered candidates
                    results = [{
                        "id": c["id"], "name": c["name"],
                        "current_company": c["current_company"],
                        "location": c["location"], "stage": c["stage"],
                        "comp": extract_comp_from_notes(c["notes"]),
                        "tags": c["tags"], "snippet": "",
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
