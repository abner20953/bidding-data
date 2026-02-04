from flask import Blueprint, render_template, request, jsonify, current_app, send_from_directory
import sqlite3
import os
import datetime
import json
import jieba
import time
import uuid
import re

# 定义 Blueprint

# Admin Password
ADMIN_PASSWORD = "108"

knowledge_bp = Blueprint('knowledge', __name__, 
                        template_folder='../templates/knowledge',
                        url_prefix='/zhishi')

DB_NAME = 'knowledge_base.db'

def get_db_path():
    base_dir = current_app.config.get('BASE_DIR')
    if not base_dir:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, '..', DB_NAME)

def get_db():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    # 1. 条目表
    c.execute('''
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT UNIQUE,
            title TEXT NOT NULL,
            type TEXT NOT NULL, 
            tags TEXT, 
            content TEXT, 
            url TEXT,
            publish_date TEXT,
            created_at TEXT,
            updated_at TEXT,
            screenshot TEXT,
            doc_number TEXT
        )
    ''')
    
    # Try adding columns if they don't exist
    try:
        c.execute("ALTER TABLE entries ADD COLUMN screenshot TEXT")
    except sqlite3.OperationalError:
        pass 
        
    try:
        c.execute("ALTER TABLE entries ADD COLUMN doc_number TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE entries ADD COLUMN uuid TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_uuid ON entries(uuid)")
    except sqlite3.OperationalError:
        pass
    
    # Backfill UUIDs
    c.execute("SELECT id FROM entries WHERE uuid IS NULL OR uuid = ''")
    rows = c.fetchall()
    for row in rows:
        uid = str(uuid.uuid4())
        c.execute("UPDATE entries SET uuid = ? WHERE id = ?", (uid, row[0]))
    
    # 2. 评论表
    c.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER,
            username TEXT,
            content TEXT,
            created_at TEXT,
            FOREIGN KEY(entry_id) REFERENCES entries(id)
        )
    ''')
    
    # 3. 标签表
    c.execute('''
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE
        )
    ''')
    
    # 4. 关联表
    c.execute('''
        CREATE TABLE IF NOT EXISTS entry_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER,
            target_id INTEGER,
            FOREIGN KEY(source_id) REFERENCES entries(id),
            FOREIGN KEY(target_id) REFERENCES entries(id)
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_relations_source ON entry_relations(source_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_relations_target ON entry_relations(source_id)')
        
    conn.commit()
    conn.close()

db_initialized = False

def get_db():
    global db_initialized
    if not db_initialized:
        init_db()
        db_initialized = True
    
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

# --- Routes ---

@knowledge_bp.route('/')
def index():
    return render_template('list.html')

@knowledge_bp.route('/view/<entry_id>')
def view_entry(entry_id):
    conn = get_db()
    # Try UUID first
    entry = conn.execute('SELECT * FROM entries WHERE uuid = ?', (entry_id,)).fetchone()
    
    # Fallback to ID for backward compatibility ONLY if not found (optional, but good for transition)
    # But user specifically wants to hide numbers. Let's support both but redirect numbers? 
    # Or just support UUID. For now, strict UUID match if entry_id is long, else try ID?
    # Simple approach: If it finds by UUID, good. If not, try ID.
    if not entry and entry_id.isdigit():
         entry = conn.execute('SELECT * FROM entries WHERE id = ?', (entry_id,)).fetchone()
    
    if not entry:
        conn.close()
        return "Not Found", 404
        
    internal_id = entry['id']
    
    comments = conn.execute('SELECT * FROM comments WHERE entry_id = ? ORDER BY id DESC', (internal_id,)).fetchall()
    # Fetch Related Entries
    relations = conn.execute('''
        SELECT t.id, t.uuid, t.title, t.type, t.doc_number 
        FROM entry_relations r
        JOIN entries t ON r.target_id = t.id
        WHERE r.source_id = ?
    ''', (internal_id,)).fetchall()
    
    conn.close()
    
    # Handle Search Highlighting
    query = request.args.get('q', '')
    highlight_tokens = []
    if query:
        seg_list = list(jieba.cut_for_search(query))
        highlight_tokens = [term.strip() for term in seg_list if term.strip()]
        if not highlight_tokens:
             highlight_tokens = [query]
        
    return render_template('detail.html', entry=dict(entry), comments=[dict(c) for c in comments], 
                           related_entries=[dict(r) for r in relations], highlight_tokens=highlight_tokens)

@knowledge_bp.route('/edit')
@knowledge_bp.route('/edit/<entry_id>')
def edit_entry(entry_id=None):
    entry = {}
    related_entries = []
    
    if entry_id:
        conn = get_db()
        entry_row = conn.execute('SELECT * FROM entries WHERE uuid = ?', (entry_id,)).fetchone()
        
        # Fallback
        if not entry_row and str(entry_id).isdigit():
            entry_row = conn.execute('SELECT * FROM entries WHERE id = ?', (entry_id,)).fetchone()
            
        if entry_row:
            entry = dict(entry_row)
            internal_id = entry['id']
            rels = conn.execute('''
                SELECT t.id, t.uuid, t.title 
                FROM entry_relations r
                JOIN entries t ON r.target_id = t.id
                WHERE r.source_id = ?
            ''', (internal_id,)).fetchall()
            related_entries = [dict(r) for r in rels]
            
        conn.close()
        
    return render_template('edit.html', entry=entry, related_entries=related_entries)

# --- APIs ---

@knowledge_bp.route('/api/list')
def api_list():
    query = request.args.get('q', '').strip()
    type_filter = request.args.get('type', '')
    tag_filter = request.args.get('tag', '')
    page = int(request.args.get('page', 1))
    per_page = 20
    offset = (page - 1) * per_page
    
    conn = get_db()
    # Ensure uuid is fetched
    sql = "SELECT id, uuid, title, type, tags, publish_date, created_at, content, screenshot, doc_number FROM entries WHERE 1=1"
    params = []
    
    if type_filter:
        sql += " AND type = ?"
        params.append(type_filter)
        
    if tag_filter:
        tags = [t.strip() for t in tag_filter.split(',') if t.strip()]
        if tags:
            tag_clauses = []
            for t in tags:
                tag_clauses.append("tags LIKE ?")
                params.append(f'%"{t}"%')
            sql += " AND (" + " OR ".join(tag_clauses) + ")" 
    
    search_terms = []
    if query:
        seg_list = list(jieba.cut_for_search(query))
        if seg_list:
            sub_clauses = []
            for term in seg_list:
                term = term.strip()
                if term:
                    search_terms.append(term)
                    sub_clauses.append(f"(title LIKE ? OR content LIKE ? OR doc_number LIKE ?)")
                    params.extend([f'%{term}%', f'%{term}%', f'%{term}%'])
            if sub_clauses:
                sql += " AND (" + " AND ".join(sub_clauses) + ")"
                
    count_sql = "SELECT COUNT(*) FROM (" + sql + ")"
    total = conn.execute(count_sql, params).fetchone()[0]
    
    order_clause = "created_at DESC"
    
    if query and search_terms:
        score_parts = []
        score_vals = []
        score_parts.append("(CASE WHEN title LIKE ? THEN 100 ELSE 0 END)")
        score_vals.append(f"%{query}%")
        score_parts.append("(CASE WHEN doc_number LIKE ? THEN 80 ELSE 0 END)")
        score_vals.append(f"%{query}%")
        score_parts.append("(CASE WHEN content LIKE ? THEN 50 ELSE 0 END)")
        score_vals.append(f"%{query}%")
        for term in search_terms:
            score_parts.append("(CASE WHEN title LIKE ? THEN 10 ELSE 0 END)")
            score_vals.append(f"%{term}%")
        score_expr = " + ".join(score_parts)
        order_clause = f"({score_expr}) DESC, created_at DESC"
        params.extend(score_vals)
    
    sql += f" ORDER BY {order_clause} LIMIT ? OFFSET ?"
    params.extend([per_page, offset])
    
    rows = conn.execute(sql, params).fetchall()
    
    # Check if UUIDs are present, backfill on the fly if needed (safety net)
    results = []
    for row in rows:
        item = dict(row)
        if not item.get('uuid'):
            # This shouldn't happen if init_db ran, but just in case
            new_uid = str(uuid.uuid4())
            conn.execute("UPDATE entries SET uuid = ? WHERE id = ?", (new_uid, item['id']))
            conn.commit()
            item['uuid'] = new_uid
            
        content = item.pop('content') or ''
        # Strip HTML tags for summary to prevent layout breakage
        clean_content = re.sub(r'<[^>]+>', '', content)
        summary = clean_content[:200]
        if query:
            content_lower = clean_content.lower()
            query_lower = query.lower()
            best_pos = content_lower.find(query_lower)
            if best_pos == -1 and search_terms:
                min_pos = -1
                for term in search_terms:
                    pos = content_lower.find(term.lower())
                    if pos != -1:
                        if min_pos == -1 or pos < min_pos:
                            min_pos = pos
                best_pos = min_pos
            if best_pos != -1:
                start = max(0, best_pos - 30)
                end = min(len(clean_content), start + 200)
                summary = ('...' if start > 0 else '') + clean_content[start:end] + ('...' if end < len(clean_content) else '')
        
        item['summary'] = summary
        results.append(item)
    
    conn.close()
    
    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "data": results,
        "search_terms": search_terms
    })



@knowledge_bp.route('/api/tags', methods=['GET', 'POST', 'PUT', 'DELETE'])
def api_tags():
    conn = get_db()
    if request.method == 'GET':
        rows = conn.execute("SELECT name FROM tags ORDER BY id").fetchall()
        conn.close()
        return jsonify([r['name'] for r in rows])
        
    if request.method == 'POST':
        name = request.get_json().get('name', '').strip()
        if not name:
            return jsonify({"error": "标签名不能为空"}), 400
        try:
            conn.execute("INSERT INTO tags (name) VALUES (?)", (name,))
            conn.commit()
            status = "success"
        except sqlite3.IntegrityError:
            status = "exists"
        conn.close()
        return jsonify({"status": status, "name": name})

    if request.method == 'PUT':
        data = request.get_json()
        old_name = data.get('old_name', '').strip()
        new_name = data.get('new_name', '').strip()
        
        if not old_name or not new_name:
            return jsonify({"error": "标签名不能为空"}), 400
            
        try:
            # 1. Update tags table
            conn.execute("UPDATE tags SET name=? WHERE name=?", (new_name, old_name))
            
            # 2. Update entries (Global Rename)
            cursor = conn.execute("SELECT id, tags FROM entries WHERE tags LIKE ?", (f'%"{old_name}"%',))
            rows = cursor.fetchall()
            
            for row in rows:
                try:
                    entry_id = row['id']
                    tags_list = json.loads(row['tags'])
                    if old_name in tags_list:
                        new_tags_list = [new_name if t == old_name else t for t in tags_list]
                        new_tags_list = list(set(new_tags_list)) 
                        conn.execute("UPDATE entries SET tags=? WHERE id=?", 
                                    (json.dumps(new_tags_list, ensure_ascii=False), entry_id))
                except:
                    pass
                    
            conn.commit()
            return jsonify({"status": "success"})
        except sqlite3.IntegrityError:
             return jsonify({"error": "该标签名已存在"}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            conn.close()

    if request.method == 'DELETE':
        data = request.get_json()
        password = data.get('password') if data else None
        
        if password != ADMIN_PASSWORD:
             return jsonify({"error": "Admin password required or invalid"}), 403

        name = data.get('name', '').strip()
        if not name:
             return jsonify({"error": "标签名不能为空"}), 400
             
        try:
            # 1. Delete from tags table
            conn.execute("DELETE FROM tags WHERE name=?", (name,))
            
            # 2. Remove from entries
            cursor = conn.execute("SELECT id, tags FROM entries WHERE tags LIKE ?", (f'%"{name}"%',))
            rows = cursor.fetchall()
            
            for row in rows:
                try:
                    entry_id = row['id']
                    tags_list = json.loads(row['tags'])
                    if name in tags_list:
                        new_tags_list = [t for t in tags_list if t != name]
                        conn.execute("UPDATE entries SET tags=? WHERE id=?", 
                                    (json.dumps(new_tags_list, ensure_ascii=False), entry_id))
                except:
                    pass
            
            conn.commit()
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            conn.close()

@knowledge_bp.route('/api/save', methods=['POST'])
def api_save():
    data = request.get_json()
    entry_id_or_uuid = data.get('id')
    title = data.get('title')
    type_val = data.get('type')
    tags = json.dumps(data.get('tags', []), ensure_ascii=False)
    content = data.get('content')
    url = data.get('url')
    publish_date = data.get('publish_date')
    screenshot = data.get('screenshot')
    doc_number = data.get('doc_number', '').strip()
    
    if not title or not content:
        return jsonify({"error": "标题和内容不能为空"}), 400
        
    conn = get_db()
    cursor = conn.cursor()
    
    # Resolve internal ID
    internal_id = None
    if entry_id_or_uuid:
        # Try UUID
        row = conn.execute("SELECT id FROM entries WHERE uuid = ?", (entry_id_or_uuid,)).fetchone()
        if not row and str(entry_id_or_uuid).isdigit():
            row = conn.execute("SELECT id FROM entries WHERE id = ?", (entry_id_or_uuid,)).fetchone()
        
        if row: 
            internal_id = row['id']
    
    # --- Uniqueness Check ---
    # 1. Check Doc Number
    if doc_number:
        sql = "SELECT id FROM entries WHERE doc_number = ?"
        params = [doc_number]
        if internal_id:
             sql += " AND id != ?"
             params.append(internal_id)
        exists = conn.execute(sql, params).fetchone()
        if exists:
            conn.close()
            return jsonify({"error": f"文号 '{doc_number}' 已存在！"}), 400
            
    # 2. Check Title
    sql = "SELECT id FROM entries WHERE title = ?"
    params = [title]
    if internal_id:
         sql += " AND id != ?"
         params.append(internal_id)
    exists = conn.execute(sql, params).fetchone()
    if exists:
        conn.close()
        return jsonify({"error": f"标题 '{title}' 已存在！"}), 400
    # ------------------------

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if internal_id:
        cursor.execute('''
            UPDATE entries SET title=?, type=?, tags=?, content=?, url=?, publish_date=?, screenshot=?, doc_number=?, updated_at=?
            WHERE id=?
        ''', (title, type_val, tags, content, url, publish_date, screenshot, doc_number, now, internal_id))
    else:
        new_uuid = str(uuid.uuid4())
        cursor.execute('''
            INSERT INTO entries (uuid, title, type, tags, content, url, publish_date, screenshot, doc_number, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (new_uuid, title, type_val, tags, content, url, publish_date, screenshot, doc_number, now, now))
        internal_id = cursor.lastrowid
        
    # --- Process Relations ---
    # Delete all existing relations for this source
    cursor.execute("DELETE FROM entry_relations WHERE source_id = ?", (internal_id,))
    
    related_ids = data.get('related_ids', [])
    if related_ids and isinstance(related_ids, list):
         for target_identifier in related_ids:
             # target_identifier could be UUID or ID (if old). 
             # We should resolve it to internal ID first.
             # Assume frontend sends UUIDs for related entries too.
             target_row = conn.execute("SELECT id FROM entries WHERE uuid = ?", (target_identifier,)).fetchone()
             if not target_row and str(target_identifier).isdigit():
                 target_row = conn.execute("SELECT id FROM entries WHERE id = ?", (target_identifier,)).fetchone()
                 
             if target_row:
                 t_id = target_row['id']
                 # Prevent self-relation
                 if t_id != internal_id:
                      cursor.execute("INSERT INTO entry_relations (source_id, target_id) VALUES (?, ?)", (internal_id, t_id))

    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@knowledge_bp.route('/api/delete', methods=['POST'])
def api_delete():
    data = request.get_json()
    if not data or data.get('password') != ADMIN_PASSWORD:
        return jsonify({"error": "Admin password required or invalid"}), 403

    uid = data.get('id')
    
    if not uid:
        return jsonify({"error": "ID is required"}), 400
        
    conn = get_db()
    try:
        # Resolve to internal ID
        row = conn.execute("SELECT id FROM entries WHERE uuid = ?", (uid,)).fetchone()
        if not row and str(uid).isdigit():
             row = conn.execute("SELECT id FROM entries WHERE id = ?", (uid,)).fetchone()
             
        if not row:
            return jsonify({"error": "Entry not found"}), 404
            
        internal_id = row['id']
        
        # Delete entry
        conn.execute("DELETE FROM entries WHERE id = ?", (internal_id,))
        # Delete comments
        conn.execute("DELETE FROM comments WHERE entry_id = ?", (internal_id,))
        # Delete relations (as source or target)
        conn.execute("DELETE FROM entry_relations WHERE source_id = ? OR target_id = ?", (internal_id, internal_id))
        
        conn.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@knowledge_bp.route('/api/comment', methods=['POST'])
def api_comment():
    data = request.get_json()
    entry_id = data.get('entry_id')
    username = data.get('username', '匿名用户')
    content = data.get('content')
    
    if not entry_id or not content:
        return jsonify({"error": "参数缺失"}), 400
        
    conn = get_db()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute('INSERT INTO comments (entry_id, username, content, created_at) VALUES (?, ?, ?, ?)',
                (entry_id, username, content, now))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@knowledge_bp.route('/api/extract', methods=['POST'])
def api_extract():
    import requests
    from bs4 import BeautifulSoup
    
    import re
    
    url = request.get_json().get('url')
    if not url:
        return jsonify({"error": "URL cannot be empty"}), 400
        
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = resp.apparent_encoding 
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # --- 1. Universal Title Extraction ---
        # Priority: h1 > og:title > title > class matches
        title = ""
        h1 = soup.find('h1')
        if h1: 
            title = h1.get_text().strip()
        
        if not title:
            og_title = soup.find('meta', property='og:title')
            if og_title: title = og_title.get('content').strip()
            
        if not title:
            if soup.title: title = soup.title.string.strip()
            
        # Clean title (remove common suffixes)
        if title:
            title = re.sub(r'[_|-].*$', '', title).strip()

        # --- 2. Universal Date Extraction ---
        publish_date = ""
        # 2.1 Meta tags
        date_metas = [
            {'name': 'pubdate'}, {'name': 'publishdate'}, {'name': 'PubDate'},
            {'property': 'article:published_time'}, {'name': 'date'}
        ]
        for meta in date_metas:
            tag = soup.find('meta', meta)
            if tag:
                publish_date = tag.get('content')
                break
        
        # 2.2 Regex in text (Fallback)
        if not publish_date:
            # Look for YYYY-MM-DD or YYYY年MM月DD日 pattern in likely areas
            date_pattern = re.compile(r'(\d{4}[-年]\d{1,2}[-月]\d{1,2}[日]?)')
            # Limit search to top of body or meta info containers
            text_sample = soup.get_text()[:2000] 
            match = date_pattern.search(text_sample)
            if match:
                publish_date = match.group(1)

        # --- 3. Universal Content Extraction (Waterfall Strategy) ---
        content = ""
        
        # Strategy A: Specific Domain Optimizations (Preserve existing logic)
        if "gov.cn" in url:
             article = soup.find(id='UCAP-CONTENT') or soup.find(class_='pages_content') or soup.find(class_='article-content') or soup.find(class_='article')
             if article: content = article.get_text('\n', strip=True)
        elif "ccgp" in url:
             content_node = soup.find(class_='vF_detail_content') or soup.find(class_='table')
             if content_node: content = content_node.get_text('\n', strip=True)

        # Strategy B: Common Content Containers (Heuristic)
        if not content:
            # List of common class/id names for main content
            common_selectors = [
                'article', '.article', '#article', 
                '.content', '#content', '.main-content',
                '.post-content', '.entry-content', '.detail-content',
                '.news_content', '.view-content',
                # Legacy / Gov Site Selectors
                '.txt', '#txt', '.news_con', '.news-con', 
                '.detail_con', '#news_content', '.zoom', 
                '.TRS_Editor', '.Section1'
            ]
            for selector in common_selectors:
                if selector.startswith('.'):
                    node = soup.find(class_=selector[1:])
                elif selector.startswith('#'):
                    node = soup.find(id=selector[1:])
                else:
                    node = soup.find(selector)
                
                if node:
                    # Validate: Should have substantial text
                    text = node.get_text('\n', strip=True)
                    if len(text) > 50:
                        content = text
                        break

        # Strategy C: Paragraph Density (Fallback)
        if not content:
            # Find all p tags, filter out short ones (nav links etc)
            ps = soup.find_all('p')
            valid_ps = [p.get_text().strip() for p in ps if len(p.get_text().strip()) > 10]
            if valid_ps:
                content = "\n".join(valid_ps)
        
        # --- 4. Doc Number Extraction (Regex) ---
        doc_number = ""
        if content or title:
            # Regex for Chinese Doc Number: 
            # e.g., 发改法规规〔2022〕1117号, 国办发[2020]15号
            # Core pattern: [Prefix] + [Bracket] + Year + [Bracket] + Number + "号"
            
            # Pattern 1: Standard
            doc_pattern = re.compile(r'([\u4e00-\u9fa5A-Za-z0-9]{2,10}[〔\[【]\d{4}[】\]〕]\d+号)')
            
            # Check Title First
            match = doc_pattern.search(title)
            if match:
                doc_number = match.group(0)
            else:
                 # Check Content (First 3000 chars)
                 match = doc_pattern.search(content[:3000])
                 if match:
                     doc_number = match.group(0)

        return jsonify({
            "status": "success",
            "data": {
                "title": title,
                "content": content,
                "publish_date": publish_date,
                "url": url,
                "doc_number": doc_number
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@knowledge_bp.route('/api/upload', methods=['POST'])
def api_upload():
    if 'file' not in request.files:
         return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
        
    if file:
        # Generate safe filename
        ext = os.path.splitext(file.filename)[1]
        filename = f"{int(time.time())}_{os.urandom(4).hex()}{ext}"
        
        # Determine absolute path to save
        # dashboard/static/uploads
        static_uploads_dir = os.path.join(current_app.root_path, 'static', 'uploads')
        if not os.path.exists(static_uploads_dir):
            os.makedirs(static_uploads_dir)
            
        save_path = os.path.join(static_uploads_dir, filename)
        file.save(save_path) # Critical Fix: Actually save the file!
        
        # Return web accessible path
        web_path = f"/static/uploads/{filename}"
        return jsonify({"status": "success", "file_path": web_path})
    
    return jsonify({"error": "Upload failed"}), 500

@knowledge_bp.route('/api/search_titles', methods=['GET'])
def api_search_titles():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
        
    conn = get_db()
    # Simple partial match
    sql = "SELECT id, uuid, title, type, doc_number FROM entries WHERE title LIKE ? ORDER BY created_at DESC LIMIT 20"
    rows = conn.execute(sql, (f'%{query}%',)).fetchall()
    conn.close()
    
    # Ensure uuid is present (backfill safety)
    results = []
    for row in rows:
        d = dict(row)
        if not d.get('uuid'):
            d['uuid'] = str(uuid.uuid4())
            # We won't save back here for perf/simplicity, assuming api_list/init_db caught most.
            # actually better to save it if we can, but let's trust init_db.
        results.append(d)
        
    return jsonify(results)
