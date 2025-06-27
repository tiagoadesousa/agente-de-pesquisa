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
            # Tenta atualizar o token
            try:
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
    # ... (código da função sem alterações) ...
    return ""

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

# ... (todas as outras rotas permanecem aqui) ...

# --- NOVA ROTA PARA SERVIR A INTERFACE ---
@app.route('/')
def serve_index():
    return send_from_directory('static', 'index.html')

# --- INICIA O SERVIDOR ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Iniciando servidor na porta {port}...")
    app.run(host='0.0.0.0', port=port)
