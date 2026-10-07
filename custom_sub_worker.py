import os
import sys
import json
import time
import requests
import re
import shutil
import glob
import uuid
import zipfile
import patoolib
import urllib.parse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import firebase_admin
from firebase_admin import credentials, firestore, db as rtdb
import pysubs2
from dotenv import load_dotenv

load_dotenv()

# --- gRPC Keepalive configuration ---
os.environ["GRPC_ARG_KEEPALIVE_TIME_MS"] = "300000"
os.environ["GRPC_ARG_KEEPALIVE_TIMEOUT_MS"] = "20000"
os.environ["GRPC_ARG_HTTP2_MIN_SENT_PING_INTERVAL_WITHOUT_DATA_MS"] = "300000"
os.environ["GRPC_ARG_HTTP2_MAX_PINGS_WITHOUT_DATA"] = "0"
os.environ["GRPC_ARG_KEEPALIVE_PERMIT_WITHOUT_CALLS"] = "0"

import google.cloud.firestore_v1.base_client as bclient
bclient._DEFAULT_CHANNEL_OPTIONS = [
    ('grpc.keepalive_time_ms', 300000),
    ('grpc.keepalive_timeout_ms', 20000),
    ('grpc.http2.min_sent_ping_interval_without_data_ms', 300000),
    ('grpc.http2.max_pings_without_data', 0),
    ('grpc.keepalive_permit_without_calls', 0),
    ('grpc.max_send_message_length', -1),
    ('grpc.max_receive_message_length', -1),
]

# --- Universal Sub Engine Integration ---
try:
    from sub_engine import (
        process_sinhala_sub,
        clean_sub_events,
        MIN_SUB_LINE_THRESHOLD,
        clear_missing_sub_alert,
        apply_spoken_sinhala,
        is_garbage_sub,
        clean_vtt_tags,
        detect_encoding,
        upload_to_github_release,
        delete_existing_sinhala_subs,
        upload_sub_to_rpm as engine_upload_sub_to_rpm
    )
except ImportError:
    MIN_SUB_LINE_THRESHOLD = 125
    def clear_missing_sub_alert(*args, **kwargs): pass
    def upload_to_github_release(*args, **kwargs): return None
    def delete_existing_sinhala_subs(*args, **kwargs): pass
    def engine_upload_sub_to_rpm(*args, **kwargs): return False

try:
    from spoken_dict import SPOKEN_DICT
except ImportError:
    SPOKEN_DICT = {}

# --- Session & Network Setup ---
session = requests.Session()
retries = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retries, pool_connections=100, pool_maxsize=100)
session.mount('https://', adapter)
session.mount('http://', adapter)

# --- Firebase Credentials ---
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL", "https://anishift-5d14b-default-rtdb.firebaseio.com/").rstrip("/")
key_path = "serviceAccountKey.json" if os.path.exists("serviceAccountKey.json") else os.path.join("..", "serviceAccountKey.json")

if os.path.exists(key_path) and not firebase_admin._apps:
    cred = credentials.Certificate(key_path)
    firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_DB_URL})
    db = firestore.client()
elif firebase_admin._apps:
    db = firestore.client()
else:
    raise RuntimeError("Missing serviceAccountKey.json for Firebase Admin SDK!")

RPM_API_TOKEN = os.getenv("RPMSHARE_API_TOKEN") 
RPM_API_TOKEN_2 = os.getenv("RPMSHARE_API_TOKEN_2")
RPM_BASE_URL = os.getenv("RPM_BASE_URL", "https://rpmshare.com/api/v1") 
DEDICATED_RTDB_URL = os.getenv("DEDICATED_RTDB_URL", "https://anihsift-sever-2-default-rtdb.firebaseio.com").rstrip("/")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")

WORKER_ID = f"CloudSub-{str(uuid.uuid4())[:4]}"

def upload_sub_to_rpm(video_id, sub_file=None, api_token=None, remote_url=None):
    token = api_token or RPM_API_TOKEN or os.getenv("RPMSHARE_API_TOKEN")
    if engine_upload_sub_to_rpm:
        return engine_upload_sub_to_rpm(video_id, sub_file, api_token=token, remote_url=remote_url, base_url=RPM_BASE_URL, log_prefix=WORKER_ID)
    return False

def find_owner_token_and_email(abyss_video_id, preferred_hint=None):
    try:
        r = requests.get(f"{DEDICATED_RTDB_URL}/abyss_accounts.json", timeout=10)
        if r.status_code != 200 or not r.json():
            return None, None
        accounts_map = r.json()
    except Exception as e:
        print(f"[{WORKER_ID}] ⚠️ Error fetching Abyss accounts: {e}", flush=True)
        return None, None

    acc_list = []
    for acc in accounts_map.values():
        if isinstance(acc, dict) and acc.get('password') and acc.get('email'):
            if preferred_hint and (acc.get('email') == preferred_hint or preferred_hint in acc.get('email', '')):
                acc_list.insert(0, acc)
            else:
                acc_list.append(acc)

    for acc in acc_list:
        email = acc.get('email')
        pwd = acc.get('password')
        try:
            res = requests.post("https://api.abyss.to/auth/login", json={"email": email, "password": pwd}, timeout=10)
            if res.status_code == 200 and res.json().get('token'):
                tok = res.json()['token']
                chk = requests.get(f"https://api.abyss.to/v1/subtitles/{abyss_video_id}/list", headers={'Authorization': f'Bearer {tok}'}, timeout=10)
                if chk.status_code == 200:
                    return tok, email
        except Exception:
            pass

    return None, None

def replace_abyss_sinhala_subtitle(abyss_video_id, sub_bytes, account_hint=None):
    jwt_token, used_email = find_owner_token_and_email(abyss_video_id, account_hint)
    if not jwt_token:
        return False, "Failed to locate owner Abyss account for this video"

    try:
        list_res = requests.get(f"https://api.abyss.to/v1/subtitles/{abyss_video_id}/list", headers={'Authorization': f'Bearer {jwt_token}'}, timeout=15)
        if list_res.status_code == 200:
            items = list_res.json().get('items', [])
            for item in items:
                name = (item.get('name') or '').lower()
                lang = (item.get('language') or '').lower()
                label = (item.get('label') or '').lower()
                title = (item.get('title') or '').lower()
                
                is_sinhala = (
                    'sinhala' in name or 'sinhala' in lang or 'sinhala' in label or 'sinhala' in title or
                    lang in ['si', 'sin', 'sinhala'] or
                    'si' in name or 'si' in label
                )
                
                if is_sinhala:
                    sid = item.get('id')
                    del_res = requests.delete(f"https://api.abyss.to/v1/subtitles/{sid}", headers={'Authorization': f'Bearer {jwt_token}'}, timeout=15)
                    print(f"[{WORKER_ID}]    🗑️ Deleted old Abyss Sinhala sub ({sid}) on account {used_email} [Status: {del_res.status_code}]", flush=True)
                    time.sleep(0.5)
    except Exception as e:
        print(f"[{WORKER_ID}]    ⚠️ Error checking/deleting old Abyss sub: {e}", flush=True)

    try:
        put_url = f"https://api.abyss.to/v1/upload/subtitles/{abyss_video_id}?language=Sinhala&filename=sinhala.srt"
        headers = {
            'Authorization': f'Bearer {jwt_token}',
            'Content-Type': 'application/octet-stream'
        }
        put_res = requests.put(put_url, headers=headers, data=sub_bytes, timeout=40)
        if put_res.status_code in [200, 201]:
            return True, used_email
        else:
            return False, f"Abyss upload returned HTTP {put_res.status_code}: {put_res.text[:80]}"
    except Exception as e:
        return False, str(e)

def extract_episode_number(text):
    clean_text = text.lower()
    clean_text = re.sub(r'\.(srt|ass|vtt|ssa|txt|mkv|mp4|avi)$', ' ', clean_text)
    clean_text = clean_text.replace('[', ' ').replace(']', ' ').replace('(', ' ').replace(')', ' ')
    clean_text = re.sub(r'\b(?:1080p|720p|480p|2160p|x264|x265|h264|hevc|10bit|8bit|av1)\b', ' ', clean_text)
    clean_text = re.sub(r'\b20\d{2}\b', ' ', clean_text) 
    clean_text = re.sub(r'\btrack\s*\d+\b', ' ', clean_text) 
    clean_text = re.sub(r'\bseason\s*\d+\b', ' ', clean_text)
    clean_text = re.sub(r'\bs\d+\b(?!e\d+)', ' ', clean_text) 
    
    match = re.search(r'[sS]\d+[eE](\d+)', clean_text)
    if match: return int(match.group(1))
    
    match = re.search(r'(?:ep|episode|e)\.?\s*0*(\d+)', clean_text)
    if match: return int(match.group(1))
    
    match = re.search(r'(?:\s-\s|_)\s*0*(\d+)(?:v\d)?\b', clean_text)
    if match: return int(match.group(1))
    
    numbers = re.findall(r'\b0*(\d{1,4})\b', clean_text)
    if numbers:
        return int(numbers[-1])
        
    return None

def select_best_track(files, target_lang='en'):
    if not files: return None
    valid_subs_data = []
    
    for f in files:
        fname = os.path.basename(f).lower()
        if 'sign' in fname or 'song' in fname: continue
        
        try:
            enc = detect_encoding(f)
            try: subs = pysubs2.load(f, encoding=enc)
            except: subs = pysubs2.load(f, encoding='latin-1')
            
            line_count = len(subs.events)
            score = line_count
            
            if line_count >= MIN_SUB_LINE_THRESHOLD:
                if target_lang == 'en':
                    if any(k in fname for k in ['eng', 'en', 'english']):
                        score += 100000
                    elif 'ja' in fname or 'jap' in fname:
                        score -= 100000
                elif target_lang == 'si':
                    if any(k in fname for k in ['si', 'sin', 'sinhala']):
                        score += 100000
            else:
                score -= 50000
                    
            valid_subs_data.append({'path': f, 'score': score, 'lines': line_count, 'name': fname})
        except Exception:
            try:
                size = os.path.getsize(f)
                valid_subs_data.append({'path': f, 'score': size, 'lines': 0, 'name': fname})
            except: pass
            
    if valid_subs_data:
        valid_subs_data.sort(key=lambda x: x['score'], reverse=True)
        winner = valid_subs_data[0]
        if winner['lines'] < MIN_SUB_LINE_THRESHOLD:
            print(f"[{WORKER_ID}]       ⚠️ Warning: Best track '{winner['name']}' has only {winner['lines']} lines (< {MIN_SUB_LINE_THRESHOLD}).")
        print(f"[{WORKER_ID}]       🏆 ZIP WINNER: '{winner['name']}' (Score: {winner['score']}, Lines: {winner['lines']})")
        return winner['path']
        
    return files[0] if files else None

def generate_english_srt(sub_path, source_lang, out_dir=None):
    if source_lang == 'si': return None 
    
    print(f"[{WORKER_ID}]    📝 Formatting English Sub (Removing Duplicates & Ghost Lines)...")
    fname = f"english_sub_{uuid.uuid4().hex[:6]}.srt"
    out_name = os.path.join(out_dir, fname) if out_dir else fname
    try:
        enc = detect_encoding(sub_path)
        try: subs = pysubs2.load(sub_path, encoding=enc)
        except: subs = pysubs2.load(sub_path, encoding='latin-1')
        
        cleaned_events, _ = clean_sub_events(subs)
        if not cleaned_events: return None

        latin_lines = sum(1 for e in cleaned_events if re.search(r'[a-zA-Z]', e.text))
        if latin_lines < len(cleaned_events) * 0.3:
            print(f"[{WORKER_ID}]    ℹ️ Source sub is not English/Latin ({latin_lines}/{len(cleaned_events)} Latin lines). Skipping English.srt.")
            return None

        subs.events = cleaned_events
        subs.save(out_name, encoding="utf-8")
        return out_name
    except: return None

def generate_sinhala_srt(sub_path, source_lang, out_dir=None):
    fname = f"sinhala_sub_{uuid.uuid4().hex[:6]}.srt"
    out_name = os.path.join(out_dir, fname) if out_dir else fname
    if source_lang == 'si':
        print(f"[{WORKER_ID}]    📝 Sinhala Source Detected: Cleaning & saving directly...")
        try:
            enc = detect_encoding(sub_path)
            try: subs = pysubs2.load(sub_path, encoding=enc)
            except: subs = pysubs2.load(sub_path, encoding='latin-1')
            cleaned_events, _ = clean_sub_events(subs)
            if not cleaned_events: return None
            subs.events = cleaned_events
            subs.save(out_name, encoding="utf-8")
            return out_name
        except Exception as e:
            print(f"[{WORKER_ID}] ⚠️ Error cleaning Sinhala sub: {e}")
            return None
    else:
        print(f"[{WORKER_ID}]    🇱🇰 Translating to Sinhala via Universal Engine...")
        try:
            return process_sinhala_sub(sub_path, out_name=out_name, log_prefix=f"[{WORKER_ID}]")
        except Exception as e:
            print(f"[{WORKER_ID}] ⚠️ Error translating to Sinhala: {e}")
            return None

def process_job(job_id, job_data):
    anilist_id = job_data.get('anilist_id')
    zip_url = job_data.get('zip_url') 
    lang = job_data.get('language', 'en')

    raw_servers = job_data.get('target_servers')
    if not raw_servers:
        ts = job_data.get('target_server')
        if ts in [1, '1']:
            raw_servers = [1]
        elif ts in [2, '2']:
            raw_servers = [2]
        else:
            raw_servers = [1, 2]
    target_servers = [int(s) for s in raw_servers if str(s) in ['1', '2']]
    if not target_servers:
        target_servers = [1, 2]
    
    print(f"\n[{WORKER_ID}] 🚀 Cloud Subtitle Job Started: Anime {anilist_id}")
    print(f"[{WORKER_ID}] 🔗 Target URL: {zip_url}")
    print(f"[{WORKER_ID}] 🎯 Target Server(s): {target_servers}")
    
    try:
        rtdb.reference(f'subtitle_jobs/{job_id}').update({'status': 'processing'})
    except Exception: pass
    
    worker_job_dir = os.path.abspath(f"temp_cloudsub_{WORKER_ID}_{job_id}_{uuid.uuid4().hex[:6]}")
    os.makedirs(worker_job_dir, exist_ok=True)
    zip_path = os.path.join(worker_job_dir, f"job_{job_id}.zip")
    extract_dir = os.path.join(worker_job_dir, "extracted")
    
    try:
        print(f"[{WORKER_ID}] 📥 Downloading ZIP from Link...", flush=True)
        parsed = urllib.parse.urlparse(zip_url)
        domain = parsed.netloc.lower()
        referer = 'https://subdl.com/' if 'subdl.com' in domain else f"{parsed.scheme}://{parsed.netloc}/"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,application/zip,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': referer,
            'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Linux"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'cross-site',
            'Upgrade-Insecure-Requests': '1'
        }
        r = None
        try:
            r = session.get(zip_url, stream=True, headers=headers, timeout=40)
            if r.status_code == 403:
                headers['Referer'] = 'https://www.google.com/'
                r = requests.get(zip_url, stream=True, headers=headers, timeout=40)
        except Exception as re_err:
            print(f"[{WORKER_ID}] ⚠️ Requests error: {re_err}. Trying curl fallback...", flush=True)

        if r and r.status_code == 200:
            with open(zip_path, 'wb') as f:
                for chunk in r.iter_content(1024): 
                    if chunk: f.write(chunk)
        else:
            http_code = r.status_code if r else 'None'
            print(f"[{WORKER_ID}] ⚠️ Requests HTTP {http_code}. Attempting fallback via curl...", flush=True)
            import subprocess
            try:
                res = subprocess.run([
                    "curl", "-sSL",
                    "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "-H", f"Referer: {referer}",
                    "-o", zip_path,
                    zip_url
                ], timeout=90, capture_output=True)
                if not (os.path.exists(zip_path) and os.path.getsize(zip_path) >= 500):
                    raise Exception(f"curl download failed: {res.stderr.decode('utf-8', errors='ignore')[:120]}")
            except Exception as ce:
                raise Exception(f"Failed to download ZIP file (HTTP {http_code}, curl error: {ce})")
            
        file_size = os.path.getsize(zip_path)
        print(f"[{WORKER_ID}] 📦 Downloaded File Size: {file_size / 1024:.2f} KB", flush=True)
        if file_size < 500:
            raise Exception("Downloaded file is too small to be a ZIP. Probably an error page or broken link.")
            
        print(f"[{WORKER_ID}] 📦 Extracting ZIP...", flush=True)
        os.makedirs(extract_dir, exist_ok=True)
        try: 
            patoolib.extract_archive(zip_path, outdir=extract_dir, verbosity=-1)
        except Exception:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref: 
                zip_ref.extractall(extract_dir)
                
        sub_files = glob.glob(os.path.join(extract_dir, '**', '*.*'), recursive=True)
        valid_exts = ['.srt', '.ass', '.vtt', '.ssa']
        ep_files_map = {} 
        
        for f in sub_files:
            if os.path.splitext(f)[1].lower() not in valid_exts: continue
            normalized_path = f.replace('\\', '/')
            path_parts = normalized_path.split('/')
            
            ep_num = None
            for part in reversed(path_parts):
                ep_num = extract_episode_number(part)
                if ep_num is not None:
                    break 
                    
            if ep_num is not None:
                if ep_num not in ep_files_map: ep_files_map[ep_num] = []
                ep_files_map[ep_num].append(f)
                
        if not ep_files_map: raise Exception("No valid subtitle files with episode numbers found in ZIP")
            
        print(f"[{WORKER_ID}] 🧩 Found subtitles for {len(ep_files_map)} episodes.")
        
        eps_docs = list(db.collection('anime_series').document(str(anilist_id)).collection('episodes').stream())
        if not eps_docs:
            eps_docs = list(db.collection('anime_movies').document(str(anilist_id)).collection('episodes').stream())
            
        success_count = 0
        for doc_ep in eps_docs:
            ep_data = doc_ep.to_dict() or {}
            db_ep_num = int(ep_data.get('episodeNumber', 0))
            links = ep_data.get('links') or {}

            # Find RPM Video ID
            rpm_vid = links.get('rpm_video_id')
            if not rpm_vid:
                for fld in ['rpm_stream', 'rpm_download']:
                    val = links.get(fld)
                    if val and isinstance(val, str):
                        m = re.search(r'rpmshare\.com/[vd]/([a-zA-Z0-9_-]+)', val)
                        if m:
                            rpm_vid = m.group(1)
                            break

            # Find Abyss Video ID
            abyss_vid = links.get('abyss_video_id')
            if not abyss_vid and links.get('abyss_embed'):
                val_embed = links.get('abyss_embed')
                if val_embed and isinstance(val_embed, str):
                    m = re.search(r'embed/([a-zA-Z0-9_-]+)', val_embed)
                    if m: abyss_vid = m.group(1)

            server_num = ep_data.get('server', 1)
            
            if db_ep_num in ep_files_map:
                best_sub = select_best_track(ep_files_map[db_ep_num], lang)
                if best_sub:
                    print(f"\n[{WORKER_ID}]    -> Ep {db_ep_num}: Processing {os.path.basename(best_sub)} [Targets: {target_servers}]", flush=True)
                    
                    en_srt = generate_english_srt(best_sub, lang, out_dir=worker_job_dir)
                    si_srt = generate_sinhala_srt(best_sub, lang, out_dir=worker_job_dir)
                    
                    rel_ctx = {}
                    en_link = upload_to_github_release(en_srt, asset_name="English.srt", release_context=rel_ctx) if en_srt else None
                    si_link = upload_to_github_release(si_srt, asset_name="Sinhala.srt", release_context=rel_ctx) if si_srt else None
                    
                    si_bytes = None
                    if si_srt and os.path.exists(si_srt):
                        try:
                            with open(si_srt, 'rb') as f:
                                si_bytes = f.read()
                        except Exception: pass

                    # 1. SERVER 1 (RPM)
                    if 1 in target_servers:
                        if rpm_vid and si_srt:
                            delete_existing_sinhala_subs(rpm_vid, RPM_API_TOKEN)
                            upload_sub_to_rpm(rpm_vid, si_srt, RPM_API_TOKEN, remote_url=si_link)
                            print(f"[{WORKER_ID}]       ✅ Server 1 (RPM): Sinhala subtitle uploaded (Video ID: {rpm_vid})", flush=True)
                        elif not rpm_vid:
                            print(f"[{WORKER_ID}]       ℹ️ Server 1: No RPM video ID found for Ep {db_ep_num}, saved sub DL link to DB.", flush=True)

                    # 2. SERVER 2 (Abyss or RPM Server 2)
                    if 2 in target_servers:
                        if abyss_vid and si_bytes:
                            acc_hint = links.get('account') or ep_data.get('account_name')
                            ok, msg = replace_abyss_sinhala_subtitle(abyss_vid, si_bytes, account_hint=acc_hint)
                            if ok:
                                print(f"[{WORKER_ID}]       ✅ Server 2 (Abyss): Sinhala subtitle attached (Account: {msg})", flush=True)
                                try:
                                    ep_str = f"ep_{int(db_ep_num):04d}"
                                    requests.patch(f"{DEDICATED_RTDB_URL}/anime_folders/{anilist_id}/videos/{ep_str}.json", json={
                                        "subtitles": {"sinhala": True}
                                    }, timeout=5)
                                except Exception: pass
                            else:
                                print(f"[{WORKER_ID}]       ⚠️ Server 2 (Abyss): Failed to push sub: {msg}", flush=True)
                        elif server_num == 2 and rpm_vid and RPM_API_TOKEN_2 and si_srt:
                            delete_existing_sinhala_subs(rpm_vid, RPM_API_TOKEN_2)
                            upload_sub_to_rpm(rpm_vid, si_srt, RPM_API_TOKEN_2, remote_url=si_link)
                            print(f"[{WORKER_ID}]       ✅ Server 2 (RPM Long): Sinhala sub uploaded (Video ID: {rpm_vid})", flush=True)
                        else:
                            print(f"[{WORKER_ID}]       ℹ️ Server 2: No Abyss/RPM S2 video ID found for Ep {db_ep_num}, saved sub DL link to DB.", flush=True)

                    # 3. SAVE SUBTITLES DOWNLOAD LINKS TO FIRESTORE DB
                    if en_link or si_link:
                        update_payload = {
                            'subtitles.sinhala': si_link if si_link else 'no_sub_available',
                            'subtitles.english': en_link if en_link else 'not_found',
                            'last_updated': firestore.SERVER_TIMESTAMP
                        }
                        if 1 in target_servers and rpm_vid:
                            update_payload['last_fixed'] = firestore.SERVER_TIMESTAMP
                            update_payload['report_status'] = 'fixed'
                        if 2 in target_servers and abyss_vid:
                            update_payload['last_fixed_server_2'] = firestore.SERVER_TIMESTAMP
                            update_payload['report_status_server_2'] = 'fixed'
                        
                        doc_ep.reference.update(update_payload)
                        success_count += 1
                        print(f"[{WORKER_ID}]       💾 DB Updated! SI DL: {si_link} | EN DL: {en_link}", flush=True)

                    if en_srt and os.path.exists(en_srt):
                        try: os.remove(en_srt)
                        except Exception: pass
                    if si_srt and os.path.exists(si_srt):
                        try: os.remove(si_srt)
                        except Exception: pass
                        
        rtdb.reference(f'subtitle_jobs/{job_id}').update({
            'status': 'completed',
            'success_episodes': success_count,
            'completed_at': int(time.time())
        })
        print(f"[{WORKER_ID}] 🎉 Job Completed! Successfully added subs for {success_count} episodes.", flush=True)
        try:
            clear_missing_sub_alert(rtdb, anilist_id)
        except Exception: pass

    except Exception as e:
        print(f"[{WORKER_ID}] ❌ Job Failed: {e}", flush=True)
        try:
            rtdb.reference(f'subtitle_jobs/{job_id}').update({
                'status': f'error: {str(e)}',
                'failed_at': int(time.time())
            })
        except Exception: pass
        sys.exit(1)
        
    finally:
        if os.path.exists(worker_job_dir):
            shutil.rmtree(worker_job_dir, ignore_errors=True)
            print(f"[{WORKER_ID}] 🧹 Storage Cleaned: Deleted worker temp files for Job {job_id}")

if __name__ == "__main__":
    raw_payload = os.environ.get("JOB_PAYLOAD", "{}")
    job_payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    job_id = job_payload.get("job_id") or f"custom_{int(time.time())}"
    print(f"🚀 [CUSTOM SUBTITLE CLOUD WORKER] Starting Job: {job_id}", flush=True)
    process_job(job_id, job_payload)
