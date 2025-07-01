# --- APLICAÇÃO PARA RENDER ---
# Esta versão está otimizada para deployment no Render.com

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import datetime
import re
import io
import os
import json
import time
import traceback
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
# Para produção, use variáveis de ambiente
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', 'AIzaSyARsGIgQfgYhcRkyVoEOyStKueDY8NRv3I')
GOOGLE_CREDENTIALS_JSON = os.environ.get('GOOGLE_CREDENTIALS_JSON')
GOOGLE_TOKEN_JSON = os.environ.get('GOOGLE_TOKEN_JSON')

DRIVE_FOLDER_NAME = "Fichamentos_Mestrado"
SAVED_ARTICLES_FILENAME = "saved_articles.json"
CROSSREF_MAILTO = "seu.email@dominio.com"
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'  # Usado apenas para execução local

# --- LOGGING E DEBUG ---
def log_error(error_msg, exception=None):
    """Log detalhado de erros"""
    print(f"[ERRO] {error_msg}")
    if exception:
        print(f"[ERRO] Exception: {str(exception)}")
        print(f"[ERRO] Traceback: {traceback.format_exc()}")

def log_info(msg):
    """Log de informações"""
    print(f"[INFO] {msg}")

# --- FUNÇÕES COMPLETAS DO AGENTE ---

def get_drive_service():
    """Configuração robusta do serviço do Google Drive para produção e desenvolvimento"""
    creds = None
    
    try:
        # MODO PRODUÇÃO: Usar variáveis de ambiente do Render
        if GOOGLE_TOKEN_JSON and GOOGLE_CREDENTIALS_JSON:
            log_info("Executando em modo PRODUÇÃO - usando variáveis de ambiente")
            try:
                # Carrega credenciais do token das variáveis de ambiente
                creds_json = json.loads(GOOGLE_TOKEN_JSON)
                creds = Credentials.from_authorized_user_info(creds_json, SCOPES)
                log_info("Credenciais carregadas das variáveis de ambiente")
            except json.JSONDecodeError as e:
                log_error("Erro ao decodificar GOOGLE_TOKEN_JSON", e)
                raise Exception("A variável de ambiente GOOGLE_TOKEN_JSON não é um JSON válido.")
        
        # MODO DESENVOLVIMENTO: Usar arquivos locais
        elif os.path.exists(TOKEN_FILE):
            log_info("Executando em modo DESENVOLVIMENTO - usando token.json local")
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        else:
            log_error("Nenhuma credencial encontrada")
            raise Exception("Credenciais do Google não encontradas. Configure as variáveis de ambiente ou execute localmente com token.json")

        # Verifica se as credenciais são válidas
        if not creds or not creds.valid:
            log_info("Credenciais inválidas ou expiradas, tentando renovar")
            if creds and creds.expired and creds.refresh_token:
                try:
                    # Renova o token
                    creds.refresh(Request())
                    log_info("Token de acesso renovado com sucesso")
                    
                    # Salva o novo token apenas em desenvolvimento
                    if not GOOGLE_TOKEN_JSON and os.path.exists('credentials.json'):
                        with open(TOKEN_FILE, 'w') as token:
                            token.write(creds.to_json())
                            log_info("Novo token salvo localmente")
                            
                except Exception as e:
                    log_error("Falha ao renovar token", e)
                    raise Exception(f"O token de acesso expirou e não pôde ser atualizado. Erro: {e}")
            else:
                # Inicia fluxo de autorização apenas em desenvolvimento
                if not GOOGLE_TOKEN_JSON:
                    if not os.path.exists('credentials.json'):
                        raise Exception("Arquivo 'credentials.json' não encontrado para iniciar o fluxo de autorização.")
                    log_info("Iniciando fluxo de autorização local")
                    flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                    creds = flow.run_local_server(port=0)
                    with open(TOKEN_FILE, 'w') as token:
                        token.write(creds.to_json())
                else:
                    raise Exception("Token inválido em produção e sem refresh_token. Reautorização necessária.")
        
        # Cria e retorna o serviço
        service = build('drive', 'v3', credentials=creds)
        log_info("Serviço do Google Drive configurado com sucesso")
        return service
        
    except Exception as e:
        log_error("Erro geral na configuração do Google Drive", e)
        raise

def get_ai_search_strategies(research_question, api_key):
    """Gera estratégias de busca usando IA com melhor tratamento de erros"""
    if not api_key or "SUA_CHAVE" in api_key:
        log_info("API key do Gemini não disponível, usando busca direta")
        return [{'query': research_question, 'rationale': 'Busca direta.', 'topic': 'Busca Direta'}]
    
    try:
        log_info(f"Gerando estratégias de busca para: {research_question}")
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        prompt = f"""
        Aja como um assistente de pesquisa sênior. Baseado na pergunta em português: "{research_question}", gere 3-5 estratégias de busca em inglês.
        
        Retorne **APENAS** um JSON válido com uma lista de objetos. Cada objeto deve ter:
        - "query": a string de busca em inglês (ex: "UX adoption" AND "SME")
        - "rationale": uma justificativa concisa em português sobre o foco da busca
        - "topic": um tópico categórico e curto em português (máximo 3 palavras, ex: "Desafios de Adoção")
        
        Exemplo de retorno:
        [
            {{"query": "user experience adoption SME", "rationale": "Busca geral sobre adoção de UX em PMEs", "topic": "Adoção UX"}},
            {{"query": "UX implementation challenges small business", "rationale": "Foco nos desafios de implementação", "topic": "Desafios"}}
        ]
        """
        
        response = model.generate_content(prompt)
        cleaned_response = response.text.strip().replace('```json', '').replace('```', '')
        
        decoded_data = json.loads(cleaned_response)
        if not isinstance(decoded_data, list):
            log_error("Resposta da IA não é uma lista válida")
            return [{'query': research_question, 'rationale': 'Falha na IA - busca direta.', 'topic': 'Busca Direta'}]
        
        log_info(f"Geradas {len(decoded_data)} estratégias de busca")
        return decoded_data
        
    except json.JSONDecodeError as e:
        log_error("Erro ao decodificar resposta da IA", e)
        return [{'query': research_question, 'rationale': 'Falha na IA - busca direta.', 'topic': 'Busca Direta'}]
    except Exception as e:
        log_error("Erro geral na geração de estratégias", e)
        return [{'query': research_question, 'rationale': 'Falha na IA - busca direta.', 'topic': 'Busca Direta'}]

def get_ai_summary(abstract, api_key):
    """Gera resumo analítico usando IA"""
    if not abstract or "resumo não disponível" in abstract.lower():
        return "Não foi possível gerar o resumo."
    
    if not api_key or "SUA_CHAVE" in api_key:
        return "API key do Gemini não disponível para gerar resumo."
    
    try:
        log_info("Gerando resumo analítico...")
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        prompt = f"""
        Analise o resumo abaixo e escreva um parágrafo em português (100-150 palavras) destacando:
        1. Problema/Objetivo da pesquisa
        2. Metodologia utilizada
        3. Principais conclusões
        
        Resumo para análise:
        ---
        {abstract}
        ---
        
        Responda apenas com o parágrafo analítico, sem formatação adicional.
        """
        
        response = model.generate_content(prompt)
        time.sleep(1)  # Rate limiting
        return response.text.strip()
        
    except Exception as e:
        log_error("Erro ao gerar resumo analítico", e)
        return "Ocorreu um erro ao tentar gerar o resumo analítico."

def search_semantic_scholar(query, min_year, min_citations):
    """Busca no Semantic Scholar com melhor tratamento de erros"""
    try:
        log_info(f"Buscando no Semantic Scholar: {query}")
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {
            'query': query,
            'limit': 20,
            'fields': 'paperId,title,authors,year,abstract,url,citationCount'
        }
        
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        
        results = response.json().get('data', [])
        articles = []
        
        for item in results:
            try:
                year = item.get('year')
                citations = item.get('citationCount', 0)
                
                if year and int(year) >= min_year and citations >= min_citations:
                    authors = []
                    if item.get('authors'):
                        authors = [a.get('name', 'N/A') for a in item.get('authors', [])]
                    
                    article = {
                        'id': item.get('paperId', ''),
                        'title': item.get('title', 'Título não disponível'),
                        'authors': authors,
                        'year': year,
                        'source': 'Semantic Scholar',
                        'citations': citations,
                        'url': item.get('url', ''),
                        'abstract': item.get('abstract', 'Resumo não disponível')
                    }
                    articles.append(article)
            except Exception as e:
                log_error(f"Erro ao processar item do Semantic Scholar: {item.get('title', 'N/A')}", e)
                continue
        
        log_info(f"Encontrados {len(articles)} artigos no Semantic Scholar")
        return articles
        
    except requests.exceptions.Timeout:
        log_error("Timeout na busca do Semantic Scholar")
        return []
    except requests.exceptions.RequestException as e:
        if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
            log_error("Limite de requisições do Semantic Scholar atingido")
        else:
            log_error("Erro na busca do Semantic Scholar", e)
        return []
    except Exception as e:
        log_error("Erro geral na busca do Semantic Scholar", e)
        return []

def search_crossref(query, min_year):
    """Busca no CrossRef com melhor tratamento de erros"""
    try:
        log_info(f"Buscando no CrossRef: {query}")
        url = "https://api.crossref.org/works"
        params = {
            'query.bibliographic': query,
            'rows': 20,
            'filter': f'from-pub-date:{min_year}-01-01',
            'mailto': CROSSREF_MAILTO
        }
        
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        
        results = response.json()['message']['items']
        articles = []
        
        for item in results:
            try:
                date_parts = item.get('created', {}).get('date-parts', [[0]])
                year_part = date_parts[0][0] if date_parts and date_parts[0] else 0
                
                if year_part >= min_year:
                    authors = []
                    if item.get('author'):
                        authors = [f"{a.get('given', '')} {a.get('family', '')}".strip() 
                                 for a in item.get('author', [])]
                    
                    title = 'Título não disponível'
                    if item.get('title') and len(item['title']) > 0:
                        title = item['title'][0]
                    
                    article = {
                        'id': item.get('DOI', ''),
                        'title': title,
                        'authors': authors,
                        'year': year_part,
                        'source': 'CrossRef',
                        'citations': item.get('is-referenced-by-count', 0),
                        'url': item.get('URL', ''),
                        'abstract': 'Resumo não disponível no CrossRef.'
                    }
                    articles.append(article)
            except Exception as e:
                log_error(f"Erro ao processar item do CrossRef: {item.get('title', 'N/A')}", e)
                continue
        
        log_info(f"Encontrados {len(articles)} artigos no CrossRef")
        return articles
        
    except Exception as e:
        log_error("Erro na busca do CrossRef", e)
        return []

def scrape_researchgate_metadata(url):
    """Extrai metadados do ResearchGate"""
    try:
        log_info(f"Extraindo metadados do ResearchGate: {url}")
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Extrai informações
        title = soup.find('h1').text.strip() if soup.find('h1') else 'Título não encontrado'
        
        authors_div = soup.find('div', class_='research-detail-header-section__authors')
        authors = [a.text for a in authors_div.find_all('a')] if authors_div else []
        
        abstract_div = soup.find('div', class_='research-detail-middle-section__abstract')
        abstract = abstract_div.find('div').text.strip() if abstract_div else 'Resumo não encontrado.'
        
        date_div = soup.find('div', string='Date of Publication')
        year = datetime.datetime.now().year
        if date_div and date_div.find_next_sibling('div'):
            try:
                year = int(date_div.find_next_sibling('div').text.split(', ')[-1])
            except:
                pass
        
        article_id = "rg-" + url.split('/')[-1]

        article = {
            'id': article_id,
            'title': title,
            'authors': authors,
            'year': year,
            'source': 'ResearchGate (Manual)',
            'citations': 0,
            'url': url,
            'abstract': abstract,
            'topic': 'Adicionado Manualmente'
        }
        
        log_info(f"Metadados extraídos: {title}")
        return article
        
    except Exception as e:
        log_error("Erro ao extrair metadados do ResearchGate", e)
        return None

def deduplicate_articles(articles, saved_articles_ids=None):
    """Remove artigos duplicados"""
    if saved_articles_ids is None:
        saved_articles_ids = set()
    
    seen = set()
    unique_articles = []
    
    for article in articles:
        # Pula artigos já salvos
        if article.get('id') in saved_articles_ids:
            continue
            
        # Cria identificador único
        identifier = article.get('url') or article.get('id') or (article.get('title') or '').lower()
        
        if identifier and identifier not in seen:
            unique_articles.append(article)
            seen.add(identifier)
    
    return unique_articles

def sanitize_filename(text):
    """Sanitiza nome de arquivo"""
    if not text:
        return "Sem_Nome"
    return re.sub(r'[\\/*?:"<>|]', "", text).strip()

def get_or_create_folder(service, folder_name):
    """Obtém ou cria pasta no Google Drive"""
    try:
        query = f"mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false"
        response = service.files().list(q=query, fields='files(id)').execute()
        
        files = response.get('files', [])
        if files:
            log_info(f"Pasta '{folder_name}' encontrada")
            return files[0].get('id')
        else:
            log_info(f"Criando pasta '{folder_name}'")
            file_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder'
            }
            folder = service.files().create(body=file_metadata, fields='id').execute()
            return folder.get('id')
    except Exception as e:
        log_error(f"Erro ao obter/criar pasta '{folder_name}'", e)
        raise

def download_file_content(service, folder_id, filename):
    """Baixa conteúdo de arquivo do Google Drive"""
    try:
        query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
        response = service.files().list(q=query, fields='files(id)').execute()
        
        files = response.get('files', [])
        if files:
            request = service.files().get_media(fileId=files[0]['id'])
            content = io.BytesIO(request.execute()).read().decode('utf-8')
            log_info(f"Arquivo '{filename}' baixado com sucesso")
            return content
        return None
    except Exception as e:
        log_error(f"Erro ao baixar arquivo '{filename}'", e)
        return None

def upload_text_file(service, folder_id, filename, content):
    """Faz upload de arquivo de texto para o Google Drive"""
    try:
        file_metadata = {'name': filename, 'parents': [folder_id]}
        media = io.BytesIO(content.encode('utf-8'))
        media_body = MediaIoBaseUpload(media, mimetype='text/plain', resumable=True)
        
        # Verifica se arquivo já existe
        query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
        response = service.files().list(q=query, fields='files(id)').execute()
        
        files = response.get('files', [])
        if files:
            # Atualiza arquivo existente
            service.files().update(fileId=files[0]['id'], media_body=media_body).execute()
            log_info(f"Arquivo '{filename}' atualizado")
        else:
            # Cria novo arquivo
            service.files().create(body=file_metadata, media_body=media_body).execute()
            log_info(f"Arquivo '{filename}' criado")
    except Exception as e:
        log_error(f"Erro ao fazer upload do arquivo '{filename}'", e)
        raise

def load_saved_articles_from_drive(service, folder_id):
    """Carrega artigos salvos do Google Drive"""
    try:
        content = download_file_content(service, folder_id, SAVED_ARTICLES_FILENAME)
        if not content:
            log_info("Nenhum arquivo de artigos salvos encontrado")
            return []
        
        articles = json.loads(content)
        log_info(f"Carregados {len(articles)} artigos salvos")
        return articles
    except json.JSONDecodeError as e:
        log_error("Erro ao decodificar arquivo de artigos salvos", e)
        return []
    except Exception as e:
        log_error("Erro ao carregar artigos salvos", e)
        return []

def save_articles_to_drive(service, folder_id, articles):
    """Salva artigos no Google Drive"""
    try:
        content = json.dumps(articles, indent=2, ensure_ascii=False)
        upload_text_file(service, folder_id, SAVED_ARTICLES_FILENAME, content)
        log_info(f"Salvos {len(articles)} artigos no Drive")
    except Exception as e:
        log_error("Erro ao salvar artigos no Drive", e)
        raise

def format_abnt(article):
    """Formata referência no padrão ABNT"""
    try:
        authors = article.get('authors', [])
        if not authors:
            author_str = "AUTOR DESCONHECIDO"
        else:
            first_author = authors[0]
            name_parts = first_author.split(' ')
            if len(name_parts) > 1:
                last_name = name_parts[-1].upper()
                given_name = " ".join(name_parts[:-1])
                author_str = f"{last_name}, {given_name}"
            else:
                author_str = first_author.upper()
            
            if len(authors) > 1:
                author_str += " et al"
        
        title = article.get('title', 'Título não disponível')
        publication_info = article.get('source', 'Fonte não disponível')
        year = article.get('year', 's.d.')
        url = article.get('url', '#')
        
        read_date_str = article.get('readDate')
        if read_date_str:
            try:
                read_date_obj = datetime.datetime.strptime(read_date_str, "%Y-%m-%d")
                meses = {
                    1: 'jan.', 2: 'fev.', 3: 'mar.', 4: 'abr.',
                    5: 'maio', 6: 'jun.', 7: 'jul.', 8: 'ago.',
                    9: 'set.', 10: 'out.', 11: 'nov.', 12: 'dez.'
                }
                access_date_str = f"Acesso em: {read_date_obj.day} {meses[read_date_obj.month]} {read_date_obj.year}."
            except (ValueError, TypeError):
                access_date_str = "Data de acesso não registrada."
        else:
            access_date_str = "Data de acesso não registrada."
        
        return f"{author_str}. {title}. {publication_info}, {year}. Disponível em: <{url}>. {access_date_str}"
    except Exception as e:
        log_error("Erro ao formatar referência ABNT", e)
        return f"Erro na formatação: {article.get('title', 'N/A')}"

# --- ROTAS DA API ---

@app.route('/api/search', methods=['POST'])
def handle_search():
    """Rota para busca de artigos"""
    try:
        log_info("Iniciando busca de artigos")
        data = request.json
        
        if not data:
            return jsonify({"error": "Dados não fornecidos"}), 400
        
        query_text = data.get('queryText', '').strip()
        if not query_text:
            return jsonify({"error": "Texto de busca é obrigatório"}), 400
        
        min_year = int(data.get('minYear', 2020))
        min_citations = int(data.get('minCitations', 10))
        search_type = data.get('searchType', 'direct')
        
        log_info(f"Parâmetros: query='{query_text}', type='{search_type}', min_year={min_year}, min_citations={min_citations}")
        
        # Gera estratégias de busca
        if search_type == 'ia':
            strategies = get_ai_search_strategies(query_text, GEMINI_API_KEY)
        else:
            strategies = [{'query': query_text, 'rationale': 'Busca direta.', 'topic': 'Busca Direta'}]
        
        if not strategies:
            return jsonify({"error": "Não foi possível gerar estratégias de busca."}), 500
        
        # Configura Google Drive
        try:
            service = get_drive_service()
            folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
            saved_articles = load_saved_articles_from_drive(service, folder_id)
            saved_ids = {a.get('id') for a in saved_articles if a.get('id')}
        except Exception as e:
            log_error("Erro na configuração do Google Drive", e)
            return jsonify({"error": f"Erro na configuração do Google Drive: {str(e)}"}), 500
        
        # Executa buscas
        all_found_articles = []
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            tasks = []
            
            for strategy in strategies:
                query = strategy.get('query', '').strip()
                if not query:
                    continue
                    
                rationale = strategy.get('rationale', 'Busca')
                topic = strategy.get('topic', 'Geral')
                
                log_info(f"Agendando busca para: {query} (Tópico: {topic})")
                
                # Agenda tarefas de busca
                tasks.append(executor.submit(search_semantic_scholar, query, min_year, min_citations))
                tasks.append(executor.submit(search_crossref, query, min_year))
            
            # Coleta resultados
            for future in tasks:
                try:
                    results = future.result(timeout=30)
                    if results:
                        # Adiciona informações de contexto
                        for r in results:
                            r['topic'] = strategy.get('rationale', 'Busca')
                        all_found_articles.extend(results)
                except Exception as e:
                    log_error("Uma tarefa de busca falhou", e)
                    continue
        
        # Remove duplicatas
        unique_articles = deduplicate_articles(all_found_articles, saved_ids)
        
        log_info(f"Busca concluída: {len(unique_articles)} artigos únicos encontrados")
        return jsonify(unique_articles)
        
    except Exception as e:
        log_error("Erro geral na busca", e)
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

@app.route('/api/import-bib', methods=['POST'])
def handle_bib_import():
    """Rota para importação de arquivos BibTeX"""
    try:
        log_info("Iniciando importação de arquivo BibTeX")
        
        if 'file' not in request.files:
            return jsonify({"error": "Nenhum ficheiro enviado."}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "Nenhum ficheiro selecionado."}), 400
        
        # Processa o arquivo BibTeX
        content = file.read().decode('utf-8')
        bib_database = bibtexparser.loads(content)
        
        articles = []
        for entry in bib_database.entries:
            try:
                authors = []
                if entry.get('author'):
                    authors = [name.strip() for name in entry.get('author', '').split(' and ')]
                
                article = {
                    'id': entry.get('doi') or entry.get('ID', ''),
                    'title': entry.get('title', 'N/A').replace('{', '').replace('}', ''),
                    'authors': authors,
                    'year': int(entry.get('year', 0)),
                    'source': f"Importado ({entry.get('journal', 'N/A')})",
                    'citations': 0,
                    'url': entry.get('url') or f"https://doi.org/{entry.get('doi')}" if entry.get('doi') else '#',
                    'abstract': entry.get('abstract', 'Resumo não disponível no ficheiro BibTeX.'),
                    'topic': 'Importado'
                }
                articles.append(article)
            except Exception as e:
                log_error(f"Erro ao processar entrada BibTeX: {entry.get('ID', 'N/A')}", e)
                continue
        
        # Remove duplicatas com artigos já salvos
        service = get_drive_service()
        folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
        saved_articles = load_saved_articles_from_drive(service, folder_id)
        saved_ids = {a.get('id') for a in saved_articles if a.get('id')}
        
        unique_articles = deduplicate_articles(articles, saved_ids)
        
        log_info(f"Importação concluída: {len(unique_articles)} artigos únicos importados")
        return jsonify(unique_articles)
        
    except Exception as e:
        log_error("Erro ao processar ficheiro BibTeX", e)
        return jsonify({"error": "O ficheiro enviado não é um BibTeX válido."}), 500

@app.route('/api/add-by-url', methods=['POST'])
def handle_add_by_url():
    """Rota para adicionar artigo por URL"""
    try:
        log_info("Iniciando adição de artigo por URL")
        data = request.json
        
        url = data.get('url', '').strip() if data else ''
        if not url:
            return jsonify({"error": "URL não fornecida."}), 400
        
        # Verifica se é ResearchGate
        if "researchgate.net" in url:
            article_data = scrape_researchgate_metadata(url)
        else:
            return jsonify({"error": "Atualmente, apenas links do ResearchGate são suportados."}), 400
        
        if not article_data:
            return jsonify({"error": "Não foi possível extrair os dados da URL."}), 500
        
        # Configura Google Drive
        service = get_drive_service()
        folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
        
        today = datetime.date.today().strftime("%Y-%m-%d")
        saved_articles = load_saved_articles_from_drive(service, folder_id)
        saved_ids = {a.get('id') for a in saved_articles if a.get('id')}
        
        # Verifica se já existe
        if article_data['id'] in saved_ids:
            return jsonify({"status": "info", "message": "Este artigo já existe no seu fichamento."})
        
        # Gera resumo analítico
        ai_summary = get_ai_summary(article_data.get('abstract'), GEMINI_API_KEY)
        
        # Cria arquivo markdown
        author_part = sanitize_filename("Autor")
        if article_data.get('authors') and len(article_data['authors']) > 0:
            author_name = article_data['authors'][0]
            if ' ' in author_name:
                author_part = sanitize_filename(author_name.split(' ')[-1])
            else:
                author_part = sanitize_filename(author_name)
        
        title_part = sanitize_filename(article_data.get('title', 'Sem_Titulo'))[:30]
        filename = f"{author_part}_{article_data.get('year', 'SD')}_{title_part}.md"
        
        file_content = f"""# {article_data.get('title', 'N/A')}

**Informações Bibliográficas:**
- **Autores:** {', '.join(article_data.get('authors', []))}
- **Ano:** {article_data.get('year', 'N/A')}
- **Citações:** {article_data.get('citations', 'N/A')}
- **Tópico:** {article_data.get('topic', 'N/A')}
- **Fonte:** {article_data.get('source', 'N/A')}
- **Link:** <{article_data.get('url', '#')}>
- **Data da Seleção:** {today}

---

## Resumo Analítico (IA)
> {ai_summary}

---

## Resumo Original
> {article_data.get('abstract', 'N/A')}

---

## Minhas Anotações
<!-- Adicione suas notas e reflexões aqui -->

"""
        
        # Upload do arquivo
        upload_text_file(service, folder_id, filename, file_content)
        
        # Atualiza dados do artigo
        article_data.update({
            'read': False,
            'readDate': None,
            'specificObjective': '',
            'selectionDate': today,
            'summary': ai_summary
        })
        
        saved_articles.append(article_data)
        save_articles_to_drive(service, folder_id, saved_articles)
        
        message = f"Artigo '{article_data['title'][:30]}...' adicionado com sucesso!"
        log_info(message)
        return jsonify({"status": "success", "message": message})
        
    except Exception as e:
        log_error("Erro ao adicionar artigo por URL", e)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/generate', methods=['POST'])
def handle_generate():
    """Rota para gerar fichamentos"""
    try:
        log_info("Iniciando geração de fichamentos")
        data = request.json
        
        if not data or 'articles' not in data:
            return jsonify({"status": "error", "message": "Artigos não fornecidos"}), 400
        
        articles_to_save = data.get('articles', [])
        if not articles_to_save:
            return jsonify({"status": "error", "message": "Nenhum artigo para salvar"}), 400
        
        # Configura Google Drive
        service = get_drive_service()
        folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
        
        today = datetime.date.today().strftime("%Y-%m-%d")
        saved_articles = load_saved_articles_from_drive(service, folder_id)
        saved_ids = {a.get('id') for a in saved_articles if a.get('id')}
        
        articles_processed = 0
        
        # Processa artigos em lotes menores para evitar timeout
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_summaries = {}
            
            for article in articles_to_save:
                if article.get('id') not in saved_ids:
                    future = executor.submit(get_ai_summary, article.get('abstract', ''), GEMINI_API_KEY)
                    future_summaries[future] = article
            
            for future in future_summaries:
                try:
                    article = future_summaries[future]
                    ai_summary = future.result(timeout=30)
                    
                    # Gera nome do arquivo
                    author_part = sanitize_filename("Autor")
                    if article.get('authors') and len(article['authors']) > 0:
                        author_name = article['authors'][0]
                        if ' ' in author_name:
                            author_part = sanitize_filename(author_name.split(' ')[-1])
                        else:
                            author_part = sanitize_filename(author_name)
                    
                    title_part = sanitize_filename(article.get('title', 'Sem_Titulo'))[:30]
                    filename = f"{author_part}_{article.get('year', 'SD')}_{title_part}.md"
                    
                    # Conteúdo do arquivo markdown
                    file_content = f"""# {article.get('title', 'N/A')}

**Informações Bibliográficas:**
- **Autores:** {', '.join(article.get('authors', []))}
- **Ano:** {article.get('year', 'N/A')}
- **Citações:** {article.get('citations', 'N/A')}
- **Tópico:** {article.get('topic', 'N/A')}
- **Fonte:** {article.get('source', 'N/A')}
- **Link:** <{article.get('url', '#')}>
- **Data da Seleção:** {today}

---

## Resumo Analítico (IA)
> {ai_summary}

---

## Resumo Original
> {article.get('abstract', 'N/A')}

---

## Minhas Anotações
<!-- Adicione suas notas e reflexões aqui -->

"""
                    
                    # Upload do arquivo
                    upload_text_file(service, folder_id, filename, file_content)
                    
                    # Atualiza dados do artigo
                    article.update({
                        'read': False,
                        'readDate': None,
                        'specificObjective': '',
                        'selectionDate': today,
                        'summary': ai_summary
                    })
                    
                    saved_articles.append(article)
                    articles_processed += 1
                    
                except Exception as e:
                    log_error(f"Erro ao processar artigo: {article.get('title', 'N/A')}", e)
                    continue
        
        # Salva lista atualizada
        if articles_processed > 0:
            save_articles_to_drive(service, folder_id, saved_articles)
        
        message = f"{articles_processed} fichamento(s) gerado(s) com sucesso!"
        log_info(message)
        return jsonify({"status": "success", "message": message})
        
    except Exception as e:
        log_error("Erro geral na geração de fichamentos", e)
        return jsonify({"status": "error", "message": f"Erro interno: {str(e)}"}), 500

@app.route('/api/manage/load', methods=['GET'])
def handle_load_saved():
    """Rota para carregar artigos salvos"""
    try:
        log_info("Carregando artigos salvos")
        service = get_drive_service()
        folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
        articles = load_saved_articles_from_drive(service, folder_id)
        return jsonify(articles), 200
    except Exception as e:
        log_error("Erro ao carregar artigos salvos", e)
        return jsonify([]), 200

@app.route('/api/manage/update', methods=['POST'])
def handle_update_saved():
    """Rota para atualizar artigos salvos"""
    try:
        log_info("Atualizando artigos salvos")
        data = request.json
        
        if not data or 'articles' not in data:
            return jsonify({"status": "error", "message": "Dados inválidos"}), 400
        
        service = get_drive_service()
        folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
        save_articles_to_drive(service, folder_id, data.get('articles'))
        
        return jsonify({"status": "success"})
    except Exception as e:
        log_error("Erro ao atualizar artigos salvos", e)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/build-framework', methods=['GET'])
def handle_build_framework():
    """Rota para construir referencial teórico"""
    try:
        log_info("Construindo referencial teórico")
        service = get_drive_service()
        folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
        saved_articles = load_saved_articles_from_drive(service, folder_id)
        
        # Filtra artigos lidos com objetivo específico
        relevant_articles = [art for art in saved_articles if art.get('read') and art.get('specificObjective')]
        
        # Agrupa por objetivo específico
        framework = defaultdict(list)
        for article in relevant_articles:
            objective = article['specificObjective']
            framework[objective].append(format_abnt(article))
        
        log_info(f"Referencial construído com {len(relevant_articles)} artigos em {len(framework)} objetivos")
        return jsonify(framework), 200
        
    except Exception as e:
        log_error("Erro ao construir referencial teórico", e)
        return jsonify({"error": str(e)}), 500

# --- ROTA PARA SERVIR O FRONTEND ---
@app.route('/')
def serve_index():
    """Serve a página principal"""
    return send_from_directory('static', 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    """Serve arquivos estáticos"""
    return send_from_directory('static', path)

# --- ROTA DE SAÚDE PARA O RENDER ---
@app.route('/health')
def health_check():
    """Endpoint de saúde para o Render"""
    return jsonify({"status": "healthy", "message": "Agente de Pesquisa funcionando"}), 200

# --- INICIA O SERVIDOR ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV') != 'production'
    
    log_info(f"Iniciando servidor na porta {port}")
    log_info(f"Modo debug: {debug}")
    log_info(f"Gemini API configurada: {'Sim' if GEMINI_API_KEY else 'Não'}")
    log_info(f"Google Drive em produção: {'Sim' if GOOGLE_TOKEN_JSON else 'Não'}")
    
    app.run(host='0.0.0.0', port=port, debug=debug)
