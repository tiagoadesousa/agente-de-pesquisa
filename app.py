# --- INSTALAÇÕES NECESSÁRIAS ---
# O Render usará o ficheiro requirements.txt para instalar tudo isto.

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import datetime
import re
import io
import os
import json
import time
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
import bibtexparser

# --- Importações de Lógica ---
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
import requests
from bs4 import BeautifulSoup

# --- INICIALIZAÇÃO DO SERVIDOR FLASK ---
app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

# --- CONFIGURAÇÕES GLOBAIS ---
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GOOGLE_CREDENTIALS_JSON = os.environ.get('GOOGLE_CREDENTIALS_JSON')
GOOGLE_TOKEN_JSON = os.environ.get('GOOGLE_TOKEN_JSON')

DRIVE_FOLDER_NAME = "Fichamentos_Mestrado"
SAVED_ARTICLES_FILENAME = "saved_articles.json"
CROSSREF_MAILTO = "seu.email@dominio.com"
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json' # Usado apenas para execução local

# --- FUNÇÕES COMPLETAS DO AGENTE ---

def get_drive_service():
    creds = None
    # Lógica para o ambiente do Render (produção)
    if GOOGLE_TOKEN_JSON and GOOGLE_CREDENTIALS_JSON:
        try:
            creds_json = json.loads(GOOGLE_TOKEN_JSON)
            creds = Credentials.from_authorized_user_info(creds_json, SCOPES)
        except json.JSONDecodeError:
            raise Exception("A variável de ambiente GOOGLE_TOKEN_JSON não é um JSON válido.")
    # Lógica para o ambiente local (desenvolvimento)
    elif os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                # Tenta atualizar o token
                if GOOGLE_CREDENTIALS_JSON:
                     creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
                     creds.refresh(Request(creds_info))
                else: # Fallback para o ficheiro local
                     creds.refresh(Request())
                print("Aviso: O token de acesso foi atualizado.")
                # Salva o novo token para uso futuro
                with open('token.json', 'w') as token:
                    token.write(creds.to_json())
            except Exception as e:
                 raise Exception(f"O token de acesso expirou e não pôde ser atualizado. Por favor, gere um novo token.json localmente e atualize a variável de ambiente GOOGLE_TOKEN_JSON no Render. Erro: {e}")
        else:
            # Inicia o fluxo de autorização se não houver token válido
            if not os.path.exists('credentials.json'):
                raise Exception("Arquivo 'credentials.json' não encontrado para iniciar o fluxo de autorização.")
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0) 
            with open('token.json', 'w') as token:
                token.write(creds.to_json())
    return build('drive', 'v3', credentials=creds)


def get_ai_search_strategies(research_question, api_key):
    if not api_key: return [{'query': research_question, 'rationale': 'Busca direta.', 'topic': 'Busca Direta'}]
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
        return json.loads(cleaned_response)
    except Exception as e: return [{'query': research_question, 'rationale': 'Falha na IA.', 'topic': 'Busca Direta'}]

def get_ai_summary(abstract, api_key):
    if not abstract or "resumo não disponível" in abstract.lower(): return "Não foi possível gerar o resumo."
    print("Gerando resumo analítico...")
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = f"""Analise o resumo e escreva um parágrafo em português (100-150 palavras) destacando: 1. Problema; 2. Metodologia; 3. Conclusão. Resumo: --- {abstract} ---"""
        response = model.generate_content(prompt); time.sleep(1); return response.text.strip()
    except Exception as e: return "Ocorreu um erro ao tentar gerar o resumo analítico."
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
            except Exception as e: print(f"Uma tarefa de busca falhou: {e}")
    return jsonify(deduplicate_articles(all_found_articles, saved_ids))

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
    
@app.route('/')
def serve_index():
    return send_from_directory('static', 'index.html')

# --- INICIA O SERVIDOR ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Iniciando servidor na porta {port}...")
    app.run(host='0.0.0.0', port=port)
