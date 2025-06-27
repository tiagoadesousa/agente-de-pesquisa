# --- INSTALAÇÕES NECESSÁRIAS ---
# Rode no seu terminal: pip install Flask Flask-Cors google-generativeai requests google-api-python-client google-auth-httplib2 google-auth-oauthlib beautifulsoup4 bibtexparser

from flask import Flask, request, jsonify
from flask_cors import CORS
import datetime
import re
import io
import os
import json
import time
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
import bibtexparser # Para importação

# --- Importações de Lógica ---
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
import requests
from bs4 import BeautifulSoup # Para ler HTML

# --- INICIALIZAÇÃO DO SERVIDOR FLASK ---
app = Flask(__name__)
CORS(app)

# --- CONFIGURAÇÕES GLOBAIS ---
GEMINI_API_KEY = 'SUA_CHAVE_API_GEMINI_AQUI'
DRIVE_FOLDER_NAME = "Fichamentos_Mestrado"
SAVED_ARTICLES_FILENAME = "saved_articles.json"
CROSSREF_MAILTO = "seu.email@dominio.com"
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'
CREDENTIALS_FILE = 'credentials.json'

# --- FUNÇÕES COMPLETAS DO AGENTE ---

def get_drive_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise Exception(f"Arquivo '{CREDENTIALS_FILE}' não encontrado.")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0) 
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
    return build('drive', 'v3', credentials=creds)

def get_ai_search_strategies(research_question, api_key):
    if not api_key or "SUA_CHAVE" in api_key:
        return [{'query': research_question, 'rationale': 'Busca direta.', 'topic': 'Busca Direta'}]
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = f"""
        Aja como um assistente de pesquisa sênior. Baseado na pergunta em português: "{research_question}", gere estratégias de busca em inglês.
        Retorne **APENAS** um JSON com uma lista de objetos. Cada objeto deve ter:
        - "query": a string de busca em inglês (ex: "UX adoption" AND "SME").
        - "rationale": uma justificativa concisa em português sobre o foco da busca.
        - "topic": um tópico categórico e curto em português (máximo 3 palavras, ex: "Desafios de Adoção").
        """
        response = model.generate_content(prompt)
        cleaned_response = response.text.strip().replace('```json', '').replace('```', '')
        decoded_data = json.loads(cleaned_response)
        if not isinstance(decoded_data, list):
            return [{'query': research_question, 'rationale': 'Falha na IA, busca direta.', 'topic': 'Busca Direta'}]
        return decoded_data
    except Exception as e:
        print(f"Erro na API do Gemini: {e}")
        return [{'query': research_question, 'rationale': 'Falha na IA, busca direta.', 'topic': 'Busca Direta'}]

def get_ai_summary(abstract, api_key):
    if not abstract or "resumo não disponível" in abstract.lower(): return "Não foi possível gerar o resumo."
    print("Gerando resumo analítico...")
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = f"""Analise o resumo e escreva um parágrafo em português (100-150 palavras) destacando: 1. Problema; 2. Metodologia; 3. Conclusão. Resumo: --- {abstract} ---"""
        response = model.generate_content(prompt); time.sleep(1); return response.text.strip()
    except Exception as e: return "Ocorreu um erro ao gerar o resumo."

def search_semantic_scholar(query, min_year, min_citations):
    try:
        url = "https://api.semanticscholar.org/graph/v1/paper/search"; params = {'query': query, 'limit': 20, 'fields': 'paperId,title,authors,year,abstract,url,citationCount'}
        response = requests.get(url, params=params, timeout=10); response.raise_for_status(); results = response.json().get('data', []); articles = []
        for item in results:
            if item.get('year') and int(item.get('year', 0)) >= min_year and item.get('citationCount', 0) >= min_citations: articles.append({'id': item.get('paperId'), 'title': item.get('title'), 'authors': [a['name'] for a in item.get('authors', [])], 'year': item.get('year'), 'source': 'Semantic Scholar', 'citations': item.get('citationCount'), 'url': item.get('url'), 'abstract': item.get('abstract')})
        return articles
    except requests.exceptions.RequestException as e:
        if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429: print("Aviso: Limite de requisições do Semantic Scholar atingido.")
        else: print(f"Erro na busca do Semantic Scholar: {e}")
        return []

def search_crossref(query, min_year):
    try:
        url = "https://api.crossref.org/works"; params = {'query.bibliographic': query, 'rows': 20, 'filter': f'from-pub-date:{min_year}-01-01', 'mailto': CROSSREF_MAILTO}
        response = requests.get(url, params=params, timeout=10); response.raise_for_status(); results = response.json()['message']['items']; articles = []
        for item in results:
            year_part = item.get('created', {}).get('date-parts', [[0]])[0][0]
            if year_part >= min_year:
                articles.append({'id': item.get('DOI'), 'title': item.get('title', ['N/A'])[0], 'authors': [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in item.get('author', [])], 'year': year_part, 'source': 'CrossRef', 'citations': item.get('is-referenced-by-count', 0), 'url': item.get('URL'), 'abstract': 'Resumo não disponível no CrossRef.'})
        return articles
    except Exception as e: print(f"Erro na busca do CrossRef: {e}"); return []
        
def scrape_researchgate_metadata(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        title = soup.find('h1').text.strip() if soup.find('h1') else 'Título não encontrado'
        authors_div = soup.find('div', class_='research-detail-header-section__authors')
        authors = [a.text for a in authors_div.find_all('a')] if authors_div else []
        abstract_div = soup.find('div', class_='research-detail-middle-section__abstract')
        abstract = abstract_div.find('div').text.strip() if abstract_div else 'Resumo não encontrado.'
        date_div = soup.find('div', string='Date of Publication')
        year = int(date_div.find_next_sibling('div').text.split(', ')[-1]) if date_div and date_div.find_next_sibling('div') else datetime.datetime.now().year
        article_id = "rg-" + url.split('/')[-1]

        return {
            'id': article_id, 'title': title, 'authors': authors, 'year': year, 'source': 'ResearchGate (Manual)',
            'citations': 0, 'url': url, 'abstract': abstract, 'topic': 'Adicionado Manualmente'
        }
    except Exception as e:
        print(f"Erro ao ler URL do ResearchGate: {e}")
        return None

def deduplicate_articles(articles, saved_articles_ids=None):
    if saved_articles_ids is None: saved_articles_ids = set()
    seen = set()
    unique_articles = []
    for article in articles:
        if article.get('id') in saved_articles_ids: continue
        identifier = article.get('url') or article.get('id') or (article.get('title') or '').lower()
        if identifier and identifier not in seen:
            unique_articles.append(article)
            seen.add(identifier)
    return unique_articles

def sanitize_filename(text): return re.sub(r'[\\/*?:"<>|]', "", text or '').strip()
def get_or_create_folder(service, folder_name):
    query = f"mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false"; response = service.files().list(q=query, fields='files(id)').execute()
    if files := response.get('files', []): return files[0].get('id')
    else: file_metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder'}; return service.files().create(body=file_metadata, fields='id').execute().get('id')
def download_file_content(service, folder_id, filename):
    query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"; response = service.files().list(q=query, fields='files(id)').execute()
    if files := response.get('files', []): request = service.files().get_media(fileId=files[0]['id']); return io.BytesIO(request.execute()).read().decode('utf-8')
    return None
def upload_text_file(service, folder_id, filename, content):
    file_metadata = {'name': filename, 'parents': [folder_id]}; media = io.BytesIO(content.encode('utf-8')); media_body = MediaIoBaseUpload(media, mimetype='text/plain', resumable=True)
    query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"; response = service.files().list(q=query, fields='files(id)').execute()
    if files := response.get('files', []): service.files().update(fileId=files[0]['id'], media_body=media_body).execute()
    else: service.files().create(body=file_metadata, media_body=media_body).execute()
def load_saved_articles_from_drive(service, folder_id):
    content = download_file_content(service, folder_id, SAVED_ARTICLES_FILENAME)
    if not content: return []
    try: return json.loads(content)
    except json.JSONDecodeError: return []
def save_articles_to_drive(service, folder_id, articles):
    upload_text_file(service, folder_id, SAVED_ARTICLES_FILENAME, json.dumps(articles, indent=2))

def format_abnt(article):
    authors = article.get('authors', [])
    if not authors: author_str = "AUTOR DESCONHECIDO"
    else:
        last_name = authors[0].split(' ')[-1].upper(); given_name = " ".join(authors[0].split(' ')[:-1]); author_str = f"{last_name}, {given_name}"
        if len(authors) > 1: author_str += " et al"
    title = article.get('title', 'Título não disponível'); publication_info = article.get('source', 'Fonte não disponível'); year = article.get('year', 's.d.')
    url = article.get('url', '#'); read_date_str = article.get('readDate')
    if read_date_str:
        try:
            read_date_obj = datetime.datetime.strptime(read_date_str, "%Y-%m-%d")
            meses = {1: 'jan.', 2: 'fev.', 3: 'mar.', 4: 'abr.', 5: 'maio', 6: 'jun.', 7: 'jul.', 8: 'ago.', 9: 'set.', 10: 'out.', 11: 'nov.', 12: 'dez.'}
            access_date_str = f"Acesso em: {read_date_obj.day} {meses[read_date_obj.month]} {read_date_obj.year}."
        except (ValueError, TypeError): access_date_str = "Data de acesso não registrada."
    else: access_date_str = "Data de acesso não registrada."
    return f"{author_str}. {title}. {publication_info}, {year}. Disponível em: <{url}>. {access_date_str}"

# --- ROTAS DA API ---

@app.route('/api/search', methods=['POST'])
def handle_search():
    data = request.json; min_year = int(data.get('minYear', 2020)); min_citations = int(data.get('minCitations', 10))
    strategies = get_ai_search_strategies(data.get('queryText'), GEMINI_API_KEY) if data.get('searchType') == 'ia' else [{'query': data.get('queryText'), 'rationale': 'Busca direta.', 'topic': 'Busca Direta'}]
    if not strategies: return jsonify({"error": "Não foi possível gerar estratégias."}), 500
    service = get_drive_service(); folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
    saved_articles = load_saved_articles_from_drive(service, folder_id); saved_ids = {a['id'] for a in saved_articles}
    all_found_articles = []; tasks = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        for strategy in strategies:
            query, rationale, topic = strategy.get('query'), strategy.get('rationale'), strategy.get('topic')
            if not query: continue
            print(f"Agendando busca para: {query} (Tópico: {topic})")
            tasks.append(executor.submit(search_semantic_scholar, query, min_year, min_citations))
            tasks.append(executor.submit(search_crossref, query, min_year))
        for future in tasks:
            try:
                results = future.result()
                for r in results: r['topic'] = rationale
                all_found_articles.extend(results)
            except Exception as e:
                print(f"Uma tarefa de busca falhou: {e}")
    return jsonify(deduplicate_articles(all_found_articles, saved_ids))

@app.route('/api/import-bib', methods=['POST'])
def handle_bib_import():
    if 'file' not in request.files: return jsonify({"error": "Nenhum ficheiro enviado."}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "Nenhum ficheiro selecionado."}), 400
    
    try:
        content = file.read().decode('utf-8')
        bib_database = bibtexparser.loads(content)
        articles = []
        for entry in bib_database.entries:
            authors = [name.strip() for name in entry.get('author', '').split(' and ')]
            articles.append({
                'id': entry.get('doi') or entry.get('ID'),
                'title': entry.get('title', 'N/A').replace('{', '').replace('}', ''),
                'authors': authors, 'year': int(entry.get('year', 0)),
                'source': f"Importado ({entry.get('journal', 'N/A')})", 'citations': 0, 
                'url': entry.get('url') or f"https://doi.org/{entry.get('doi')}" if entry.get('doi') else '#',
                'abstract': entry.get('abstract', 'Resumo não disponível no ficheiro BibTeX.'),
                'topic': 'Importado'
            })
        
        service = get_drive_service(); folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
        saved_articles = load_saved_articles_from_drive(service, folder_id); saved_ids = {a['id'] for a in saved_articles}
        
        return jsonify(deduplicate_articles(articles, saved_ids))
    except Exception as e:
        print(f"Erro ao processar ficheiro BibTeX: {e}")
        return jsonify({"error": "O ficheiro enviado não é um BibTeX válido."}), 500

@app.route('/api/add-by-url', methods=['POST'])
def handle_add_by_url():
    url = request.json.get('url')
    if not url: return jsonify({"error": "URL não fornecida."}), 400
    if "researchgate.net" in url:
        article_data = scrape_researchgate_metadata(url)
    else:
        return jsonify({"error": "Atualmente, apenas links do ResearchGate são suportados."}), 400
    if not article_data: return jsonify({"error": "Não foi possível extrair os dados da URL."}), 500
    try:
        service = get_drive_service(); folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
        today = datetime.date.today().strftime("%Y-%m-%d"); saved_articles = load_saved_articles_from_drive(service, folder_id)
        saved_ids = {a['id'] for a in saved_articles}
        if article_data['id'] in saved_ids:
            return jsonify({"status": "info", "message": "Este artigo já existe no seu fichamento."})
        
        ai_summary = get_ai_summary(article_data.get('abstract'), GEMINI_API_KEY)
        author_part = sanitize_filename(article_data.get('authors', ['N/A'])[0].split(' ')[-1] if article_data.get('authors') else 'Autor')
        filename = f"{author_part}_{article_data.get('year', 'SD')}_{sanitize_filename(article_data.get('title'))[:30]}.md"
        file_content = f"""# {article_data.get('title', 'N/A')}\n- Autores: {', '.join(article_data.get('authors', []))}\n- Ano: {article_data.get('year')}\n- Citações: {article_data.get('citations')}\n- Tópico: {article_data.get('topic')}\n- Fonte: {article_data.get('source')}\n- Link: <{article_data.get('url', '#')}>\n- Data da Seleção: {today}\n\n---\n\n## Resumo Analítico (IA)\n> {ai_summary}\n\n---\n\n## Resumo Original\n> {article_data.get('abstract') or 'N/A'}\n\n---\n\n## Minhas Anotações\n<!-- Adicione suas notas aqui -->"""
        upload_text_file(service, folder_id, filename, file_content)
        
        article_data.update({'read': False, 'readDate': None, 'specificObjective': '', 'selectionDate': today, 'summary': ai_summary})
        saved_articles.append(article_data)
        save_articles_to_drive(service, folder_id, saved_articles)
        return jsonify({"status": "success", "message": f"Artigo '{article_data['title'][:30]}...' adicionado com sucesso!"})
    except Exception as e:
        print(f"Erro em /api/add-by-url: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/generate', methods=['POST'])
def handle_generate():
    articles_to_save = request.json.get('articles', [])
    try:
        service = get_drive_service(); folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
        today = datetime.date.today().strftime("%Y-%m-%d"); saved_articles = load_saved_articles_from_drive(service, folder_id)
        saved_ids = {a['id'] for a in saved_articles}
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_summaries = {executor.submit(get_ai_summary, article.get('abstract'), GEMINI_API_KEY): article for article in articles_to_save if article.get('id') not in saved_ids}
            for future in future_summaries:
                article = future_summaries[future]; ai_summary = future.result()
                author_part = sanitize_filename(article.get('authors', ['N/A'])[0].split(' ')[-1] if article.get('authors') else 'Autor')
                filename = f"{author_part}_{article.get('year', 'SD')}_{sanitize_filename(article.get('title'))[:30]}.md"
                file_content = f"""# {article.get('title', 'N/A')}\n- Autores: {', '.join(article.get('authors', []))}\n- Ano: {article.get('year')}\n- Citações: {article.get('citations')}\n- Tópico: {article.get('topic')}\n- Fonte: {article.get('source')}\n- Link: <{article.get('url', '#')}>\n- Data da Seleção: {today}\n\n---\n\n## Resumo Analítico (IA)\n> {ai_summary}\n\n---\n\n## Resumo Original\n> {article.get('abstract') or 'N/A'}\n\n---\n\n## Minhas Anotações\n<!-- Adicione suas notas aqui -->"""
                upload_text_file(service, folder_id, filename, file_content)
                article.update({'read': False, 'readDate': None, 'specificObjective': '', 'selectionDate': today, 'summary': ai_summary})
                saved_articles.append(article)
        save_articles_to_drive(service, folder_id, saved_articles)
        return jsonify({"status": "success", "message": f"{len(future_summaries)} fichamentos salvos!"})
    except Exception as e: print(f"Erro em /api/generate: {e}"); return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/manage/load', methods=['GET'])
def handle_load_saved():
    try:
        service = get_drive_service(); folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
        return jsonify(load_saved_articles_from_drive(service, folder_id)), 200
    except Exception as e: print(f"Erro em /api/manage/load: {e}"); return jsonify([]), 200

@app.route('/api/manage/update', methods=['POST'])
def handle_update_saved():
    try:
        service = get_drive_service(); folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
        save_articles_to_drive(service, folder_id, request.json.get('articles'))
        return jsonify({"status": "success"})
    except Exception as e: print(f"Erro em /api/manage/update: {e}"); return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/build-framework', methods=['GET'])
def handle_build_framework():
    try:
        service = get_drive_service(); folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
        saved_articles = load_saved_articles_from_drive(service, folder_id)
        relevant_articles = [art for art in saved_articles if art.get('read') and art.get('specificObjective')]
        framework = defaultdict(list)
        for article in relevant_articles:
            objective = article['specificObjective']; framework[objective].append(format_abnt(article))
        return jsonify(framework), 200
    except Exception as e: print(f"Erro em /api/build-framework: {e}"); return jsonify({"error": str(e)}), 500

# --- INICIA O SERVIDOR ---
if __name__ == '__main__':
    print("Iniciando servidor do Agente de Pesquisa..."); print("Acesse a interface abrindo o arquivo index.html no seu navegador.")
    app.run(debug=True, port=5000)

