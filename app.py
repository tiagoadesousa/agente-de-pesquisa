# --- APLICAÇÃO PARA RENDER COM MÚLTIPLAS FONTES ACADÊMICAS ---
# Versão corrigida e otimizada para deploy

import os
import datetime
import re
import io
import json
import time
import traceback
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from urllib.parse import quote_plus, urlencode

# Framework e APIs
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import bibtexparser
import requests
from bs4 import BeautifulSoup

# Google APIs
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

# --- INICIALIZAÇÃO DO SERVIDOR FLASK ---
app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

# --- CONFIGURAÇÕES GLOBAIS SEGURAS ---
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GOOGLE_CREDENTIALS_JSON = os.environ.get('GOOGLE_CREDENTIALS_JSON')
GOOGLE_TOKEN_JSON = os.environ.get('GOOGLE_TOKEN_JSON')

# APIs de Fontes Acadêmicas
WOS_API_KEY = os.environ.get('WOS_API_KEY')
CORE_API_KEY = os.environ.get('CORE_API_KEY')
OPENALEX_EMAIL = os.environ.get('OPENALEX_EMAIL', 'seu.email@dominio.com')

# Configurações fixas
DRIVE_FOLDER_NAME = "Fichamentos_Mestrado"
SAVED_ARTICLES_FILENAME = "saved_articles.json"
CROSSREF_MAILTO = "seu.email@dominio.com"
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

# --- LOGGING E DEBUG ---
def log_error(error_msg, exception=None):
    print(f"[ERRO] {error_msg}")
    if exception:
        print(f"[ERRO] Exception: {str(exception)}")
        print(f"[ERRO] Traceback: {traceback.format_exc()}")

def log_info(msg):
    print(f"[INFO] {msg}")

# --- VALIDAÇÃO DE CONFIGURAÇÃO ---
def validate_config():
    """Valida se as configurações necessárias estão presentes"""
    missing_configs = []
    
    if not GEMINI_API_KEY:
        missing_configs.append("GEMINI_API_KEY")
    
    if not GOOGLE_TOKEN_JSON and not os.path.exists(TOKEN_FILE):
        missing_configs.append("GOOGLE_TOKEN_JSON ou token.json local")
    
    if missing_configs:
        log_error(f"Configurações obrigatórias ausentes: {', '.join(missing_configs)}")
        log_info("Configure as variáveis de ambiente no Render")
    
    # Log de APIs opcionais
    optional_apis = {
        'Web of Science': WOS_API_KEY,
        'CORE': CORE_API_KEY
    }
    
    for api_name, api_key in optional_apis.items():
        status = "✅ Configurada" if api_key else "⚠️ Não configurada (opcional)"
        log_info(f"{api_name}: {status}")

# --- FUNÇÕES DE BUSCA ACADÊMICA ---

def search_semantic_scholar(query, min_year, min_citations):
    """Busca no Semantic Scholar"""
    try:
        log_info(f"Buscando no Semantic Scholar: {query}")
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {
            'query': query,
            'limit': 20,
            'fields': 'paperId,title,authors,year,abstract,url,citationCount,venue'
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
                        'id': f"ss_{item.get('paperId', '')}",
                        'title': item.get('title', 'Título não disponível'),
                        'authors': authors,
                        'year': year,
                        'source': f"Semantic Scholar ({item.get('venue', 'N/A')})",
                        'citations': citations,
                        'url': item.get('url', ''),
                        'abstract': item.get('abstract', 'Resumo não disponível'),
                        'venue': item.get('venue', 'N/A')
                    }
                    articles.append(article)
            except Exception as e:
                log_error(f"Erro ao processar item do Semantic Scholar: {item.get('title', 'N/A')}", e)
                continue
        
        log_info(f"Semantic Scholar: {len(articles)} artigos encontrados")
        return articles
        
    except Exception as e:
        log_error("Erro na busca do Semantic Scholar", e)
        return []

def search_crossref(query, min_year):
    """Busca no CrossRef"""
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
                    
                    journal_info = item.get('container-title', ['N/A'])[0] if item.get('container-title') else 'N/A'
                    
                    article = {
                        'id': f"cr_{item.get('DOI', '')}",
                        'title': title,
                        'authors': authors,
                        'year': year_part,
                        'source': f"CrossRef ({journal_info})",
                        'citations': item.get('is-referenced-by-count', 0),
                        'url': item.get('URL', ''),
                        'abstract': 'Resumo não disponível no CrossRef.',
                        'venue': journal_info
                    }
                    articles.append(article)
            except Exception as e:
                log_error(f"Erro ao processar item do CrossRef: {item.get('title', 'N/A')}", e)
                continue
        
        log_info(f"CrossRef: {len(articles)} artigos encontrados")
        return articles
        
    except Exception as e:
        log_error("Erro na busca do CrossRef", e)
        return []

def search_web_of_science(query, min_year, min_citations):
    """Busca na Web of Science com tratamento robusto de erros"""
    if not WOS_API_KEY:
        log_info("Web of Science: API key não configurada")
        return []
    
    try:
        log_info(f"Buscando na Web of Science: {query}")
        
        endpoints = [
            "https://api.clarivate.com/apis/wos-starter/v1/documents",
            "https://api.clarivate.com/api/wos",
            "https://wos-api.clarivate.com/api/wos"
        ]
        
        headers = {
            'X-ApiKey': WOS_API_KEY,
            'Accept': 'application/json',
            'User-Agent': 'Academic-Research-Tool/1.0'
        }
        
        params = {
            'q': query,
            'db': 'WOS',
            'limit': 20,
            'sortBy': 'relevance'
        }
        
        articles = []
        
        for endpoint in endpoints:
            try:
                log_info(f"Tentando endpoint: {endpoint}")
                response = requests.get(endpoint, headers=headers, params=params, timeout=20)
                
                if response.status_code == 200:
                    data = response.json()
                    
                    if 'documents' in data:
                        items = data['documents']
                    elif 'records' in data:
                        items = data['records']
                    elif 'Data' in data and 'Records' in data['Data']:
                        items = data['Data']['Records']
                    else:
                        log_info("Web of Science: Formato de resposta não reconhecido")
                        continue
                    
                    for item in items:
                        try:
                            title = item.get('title', item.get('Title', 'Título não disponível'))
                            if isinstance(title, list):
                                title = title[0] if title else 'Título não disponível'
                            
                            authors = []
                            author_fields = ['authors', 'Authors', 'author', 'Author']
                            for field in author_fields:
                                if field in item:
                                    author_data = item[field]
                                    if isinstance(author_data, list):
                                        authors = [str(a) for a in author_data]
                                    elif isinstance(author_data, str):
                                        authors = [author_data]
                                    break
                            
                            year = item.get('publishedYear', item.get('year', item.get('Year', datetime.datetime.now().year)))
                            try:
                                year = int(year)
                            except:
                                year = datetime.datetime.now().year
                            
                            citations = item.get('citationCount', item.get('citations', item.get('Citations', 0)))
                            try:
                                citations = int(citations)
                            except:
                                citations = 0
                            
                            if year < min_year or citations < min_citations:
                                continue
                            
                            venue = item.get('journal', item.get('Journal', item.get('venue', 'N/A')))
                            abstract = item.get('abstract', item.get('Abstract', 'Resumo não disponível na Web of Science.'))
                            doi = item.get('doi', item.get('DOI', ''))
                            url = f"https://doi.org/{doi}" if doi else item.get('url', '')
                            
                            article = {
                                'id': f"wos_{doi or item.get('id', str(time.time()))}",
                                'title': title,
                                'authors': authors,
                                'year': year,
                                'source': f"Web of Science ({venue})",
                                'citations': citations,
                                'url': url,
                                'abstract': abstract,
                                'venue': venue
                            }
                            articles.append(article)
                            
                        except Exception as e:
                            log_error(f"Erro ao processar item da Web of Science: {e}")
                            continue
                    
                    log_info(f"Web of Science: {len(articles)} artigos encontrados")
                    return articles
                    
                elif response.status_code == 429:
                    log_error("Web of Science: Rate limit excedido")
                    time.sleep(2)
                    continue
                elif response.status_code == 401:
                    log_error("Web of Science: API key inválida")
                    break
                elif response.status_code == 512:
                    log_error("Web of Science: Erro interno do servidor (512)")
                    continue
                else:
                    log_error(f"Web of Science: HTTP {response.status_code}")
                    continue
                    
            except requests.exceptions.Timeout:
                log_error(f"Web of Science: Timeout no endpoint {endpoint}")
                continue
            except Exception as e:
                log_error(f"Web of Science: Erro no endpoint {endpoint}", e)
                continue
        
        log_info("Web of Science: Todos os endpoints falharam")
        return []
        
    except Exception as e:
        log_error("Erro geral na busca da Web of Science", e)
        return []

def search_doaj(query, min_year):
    """Busca no Directory of Open Access Journals (DOAJ)"""
    try:
        log_info(f"Buscando no DOAJ: {query}")
        
        url = "https://doaj.org/api/search/articles/title,abstract,subject,keyword,fulltext"
        
        params = {
            'q': query,
            'pageSize': 20,
            'sort': 'title'
        }
        
        headers = {
            'Accept': 'application/json',
            'User-Agent': 'Academic-Research-Tool/1.0'
        }
        
        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        articles = []
        
        results = data.get('results', [])
        
        for item in results:
            try:
                bibjson = item.get('bibjson', {})
                
                title = bibjson.get('title', 'Título não disponível')
                
                authors = []
                author_list = bibjson.get('author', [])
                for author in author_list:
                    name = author.get('name', '')
                    if name:
                        authors.append(name)
                
                year = None
                if bibjson.get('year'):
                    try:
                        year = int(bibjson['year'])
                    except:
                        pass
                
                if not year:
                    date_parts = bibjson.get('month', '').split('-')
                    if len(date_parts) > 0:
                        try:
                            year = int(date_parts[0])
                        except:
                            year = datetime.datetime.now().year
                    else:
                        year = datetime.datetime.now().year
                
                if year < min_year:
                    continue
                
                journal = bibjson.get('journal', {})
                journal_title = journal.get('title', 'N/A')
                
                abstract = bibjson.get('abstract', 'Resumo não disponível no DOAJ.')
                
                links = bibjson.get('link', [])
                url = ''
                for link in links:
                    if link.get('type') == 'fulltext':
                        url = link.get('url', '')
                        break
                
                if not url and bibjson.get('identifier'):
                    for identifier in bibjson['identifier']:
                        if identifier.get('type') == 'doi':
                            url = f"https://doi.org/{identifier.get('id')}"
                            break
                
                article = {
                    'id': f"doaj_{item.get('id', str(time.time()))}",
                    'title': title,
                    'authors': authors,
                    'year': year,
                    'source': f"DOAJ ({journal_title})",
                    'citations': 0,
                    'url': url,
                    'abstract': abstract,
                    'venue': journal_title
                }
                articles.append(article)
                
            except Exception as e:
                log_error(f"Erro ao processar item do DOAJ: {e}")
                continue
        
        log_info(f"DOAJ: {len(articles)} artigos encontrados")
        return articles
        
    except Exception as e:
        log_error("Erro na busca do DOAJ", e)
        return []

def search_arxiv(query, min_year):
    """Busca no arXiv"""
    try:
        log_info(f"Buscando no arXiv: {query}")
        
        base_url = "http://export.arxiv.org/api/query"
        
        params = {
            'search_query': f'all:{query}',
            'start': 0,
            'max_results': 20,
            'sortBy': 'relevance',
            'sortOrder': 'descending'
        }
        
        response = requests.get(base_url, params=params, timeout=15)
        response.raise_for_status()
        
        root = ET.fromstring(response.content)
        namespace = {'atom': 'http://www.w3.org/2005/Atom', 'arxiv': 'http://arxiv.org/schemas/atom'}
        
        articles = []
        entries = root.findall('atom:entry', namespace)
        
        for entry in entries:
            try:
                title_elem = entry.find('atom:title', namespace)
                title = title_elem.text.strip() if title_elem is not None else 'Título não disponível'
                
                authors = []
                author_elems = entry.findall('atom:author', namespace)
                for author_elem in author_elems:
                    name_elem = author_elem.find('atom:name', namespace)
                    if name_elem is not None:
                        authors.append(name_elem.text.strip())
                
                published_elem = entry.find('atom:published', namespace)
                year = datetime.datetime.now().year
                if published_elem is not None:
                    try:
                        published_date = datetime.datetime.strptime(published_elem.text[:10], '%Y-%m-%d')
                        year = published_date.year
                    except:
                        pass
                
                if year < min_year:
                    continue
                
                summary_elem = entry.find('atom:summary', namespace)
                abstract = summary_elem.text.strip() if summary_elem is not None else 'Resumo não disponível'
                
                id_elem = entry.find('atom:id', namespace)
                url = id_elem.text if id_elem is not None else ''
                
                category_elem = entry.find('atom:category', namespace)
                category = category_elem.get('term') if category_elem is not None else 'N/A'
                
                article = {
                    'id': f"arxiv_{url.split('/')[-1] if url else str(time.time())}",
                    'title': title,
                    'authors': authors,
                    'year': year,
                    'source': f"arXiv ({category})",
                    'citations': 0,
                    'url': url,
                    'abstract': abstract,
                    'venue': 'arXiv Preprint'
                }
                articles.append(article)
                
            except Exception as e:
                log_error(f"Erro ao processar item do arXiv: {e}")
                continue
        
        log_info(f"arXiv: {len(articles)} artigos encontrados")
        return articles
        
    except Exception as e:
        log_error("Erro na busca do arXiv", e)
        return []

def search_openalex(query, min_year, min_citations):
    """Busca no OpenAlex"""
    try:
        log_info(f"Buscando no OpenAlex: {query}")
        
        url = "https://api.openalex.org/works"
        
        params = {
            'search': query,
            'filter': f'publication_year:>{min_year-1},cited_by_count:>{min_citations-1}',
            'per-page': 20,
            'sort': 'cited_by_count:desc',
            'mailto': OPENALEX_EMAIL
        }
        
        headers = {
            'Accept': 'application/json',
            'User-Agent': f'Academic-Research-Tool/1.0 (mailto:{OPENALEX_EMAIL})'
        }
        
        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        articles = []
        
        results = data.get('results', [])
        
        for item in results:
            try:
                title = item.get('title', 'Título não disponível')
                
                authors = []
                authorships = item.get('authorships', [])
                for authorship in authorships:
                    author = authorship.get('author', {})
                    display_name = author.get('display_name', '')
                    if display_name:
                        authors.append(display_name)
                
                year = item.get('publication_year', datetime.datetime.now().year)
                citations = item.get('cited_by_count', 0)
                
                if year < min_year or citations < min_citations:
                    continue
                
                host_venue = item.get('host_venue', {}) or {}
                venue = host_venue.get('display_name', 'N/A')
                
                abstract_url = item.get('abstract_inverted_index')
                abstract = 'Resumo não disponível no OpenAlex.'
                
                if abstract_url and isinstance(abstract_url, dict):
                    try:
                        words = []
                        for word, positions in abstract_url.items():
                            for pos in positions:
                                while len(words) <= pos:
                                    words.append('')
                                words[pos] = word
                        abstract = ' '.join(words).strip()
                        if len(abstract) > 500:
                            abstract = abstract[:500] + '...'
                    except:
                        pass
                
                url_item = item.get('doi', '')
                if url_item:
                    url_item = f"https://doi.org/{url_item.replace('https://doi.org/', '')}"
                else:
                    url_item = item.get('id', '')
                
                article = {
                    'id': f"oa_{item.get('id', '').split('/')[-1]}",
                    'title': title,
                    'authors': authors,
                    'year': year,
                    'source': f"OpenAlex ({venue})",
                    'citations': citations,
                    'url': url_item,
                    'abstract': abstract,
                    'venue': venue
                }
                articles.append(article)
                
            except Exception as e:
                log_error(f"Erro ao processar item do OpenAlex: {e}")
                continue
        
        log_info(f"OpenAlex: {len(articles)} artigos encontrados")
        return articles
        
    except Exception as e:
        log_error("Erro na busca do OpenAlex", e)
        return []

def search_pubmed(query, min_year):
    """Busca no PubMed via API do NCBI"""
    try:
        log_info(f"Buscando no PubMed: {query}")
        
        search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        search_params = {
            'db': 'pubmed',
            'term': f"{query} AND {min_year}:3000[pdat]",
            'retmax': 20,
            'retmode': 'json'
        }
        
        search_response = requests.get(search_url, params=search_params, timeout=15)
        search_response.raise_for_status()
        
        search_data = search_response.json()
        ids = search_data.get('esearchresult', {}).get('idlist', [])
        
        if not ids:
            log_info("PubMed: Nenhum resultado encontrado")
            return []
        
        fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        fetch_params = {
            'db': 'pubmed',
            'id': ','.join(ids),
            'retmode': 'xml'
        }
        
        fetch_response = requests.get(fetch_url, params=fetch_params, timeout=15)
        fetch_response.raise_for_status()
        
        root = ET.fromstring(fetch_response.content)
        articles = []
        
        for pubmed_article in root.findall('.//PubmedArticle'):
            try:
                title_elem = pubmed_article.find('.//ArticleTitle')
                title = title_elem.text if title_elem is not None else 'Título não disponível'
                
                authors = []
                author_list = pubmed_article.findall('.//Author')
                for author in author_list:
                    lastname = author.find('LastName')
                    forename = author.find('ForeName')
                    if lastname is not None and forename is not None:
                        authors.append(f"{forename.text} {lastname.text}")
                    elif lastname is not None:
                        authors.append(lastname.text)
                
                year_elem = pubmed_article.find('.//PubDate/Year')
                year = datetime.datetime.now().year
                if year_elem is not None:
                    try:
                        year = int(year_elem.text)
                    except:
                        pass
                
                journal_elem = pubmed_article.find('.//Journal/Title')
                journal = journal_elem.text if journal_elem is not None else 'N/A'
                
                abstract_elem = pubmed_article.find('.//AbstractText')
                abstract = abstract_elem.text if abstract_elem is not None else 'Resumo não disponível no PubMed.'
                
                pmid_elem = pubmed_article.find('.//PMID')
                pmid = pmid_elem.text if pmid_elem is not None else ''
                url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ''
                
                article = {
                    'id': f"pm_{pmid}",
                    'title': title,
                    'authors': authors,
                    'year': year,
                    'source': f"PubMed ({journal})",
                    'citations': 0,
                    'url': url,
                    'abstract': abstract,
                    'venue': journal
                }
                articles.append(article)
                
            except Exception as e:
                log_error(f"Erro ao processar item do PubMed: {e}")
                continue
        
        log_info(f"PubMed: {len(articles)} artigos encontrados")
        return articles
        
    except Exception as e:
        log_error("Erro na busca do PubMed", e)
        return []

def search_core(query, min_year):
    """Busca no CORE"""
    if not CORE_API_KEY:
        log_info("CORE: API key não configurada")
        return []
    
    try:
        log_info(f"Buscando no CORE: {query}")
        
        url = "https://api.core.ac.uk/v3/search/works"
        
        headers = {
            'Authorization': f'Bearer {CORE_API_KEY}',
            'Accept': 'application/json'
        }
        
        params = {
            'q': query,
            'limit': 20,
            'scroll': False
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        articles = []
        
        results = data.get('results', [])
        
        for item in results:
            try:
                title = item.get('title', 'Título não disponível')
                
                authors = []
                author_list = item.get('authors', [])
                for author in author_list:
                    name = author.get('name', '')
                    if name:
                        authors.append(name)
                
                year = item.get('yearPublished')
                if year:
                    try:
                        year = int(year)
                    except:
                        year = datetime.datetime.now().year
                else:
                    year = datetime.datetime.now().year
                
                if year < min_year:
                    continue
                
                abstract = item.get('abstract', 'Resumo não disponível no CORE.')
                
                url = item.get('downloadUrl', item.get('sourceFulltextUrls', [''])[0] if item.get('sourceFulltextUrls') else '')
                
                journal = item.get('journals', [{}])[0].get('title', 'N/A') if item.get('journals') else 'N/A'
                
                article = {
                    'id': f"core_{item.get('id', str(time.time()))}",
                    'title': title,
                    'authors': authors,
                    'year': year,
                    'source': f"CORE ({journal})",
                    'citations': 0,
                    'url': url,
                    'abstract': abstract,
                    'venue': journal
                }
                articles.append(article)
                
            except Exception as e:
                log_error(f"Erro ao processar item do CORE: {e}")
                continue
        
        log_info(f"CORE: {len(articles)} artigos encontrados")
        return articles
        
    except Exception as e:
        log_error("Erro na busca do CORE", e)
        return []

def search_all_sources(query, min_year, min_citations, selected_sources=None):
    """Busca em todas as fontes acadêmicas disponíveis"""
    
    if selected_sources is None:
        selected_sources = [
            'semantic_scholar', 'crossref', 'doaj', 'arxiv', 
            'openalex', 'pubmed', 'web_of_science', 'core'
        ]
    
    search_functions = {
        'semantic_scholar': lambda: search_semantic_scholar(query, min_year, min_citations),
        'crossref': lambda: search_crossref(query, min_year),
        'web_of_science': lambda: search_web_of_science(query, min_year, min_citations),
        'doaj': lambda: search_doaj(query, min_year),
        'arxiv': lambda: search_arxiv(query, min_year),
        'openalex': lambda: search_openalex(query, min_year, min_citations),
        'pubmed': lambda: search_pubmed(query, min_year),
        'core': lambda: search_core(query, min_year)
    }
    
    all_articles = []
    
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_source = {}
        
        for source in selected_sources:
            if source in search_functions:
                future = executor.submit(search_functions[source])
                future_to_source[future] = source
        
        for future in as_completed(future_to_source, timeout=60):
            source = future_to_source[future]
            try:
                results = future.result(timeout=30)
                log_info(f"{source.title()}: {len(results)} artigos coletados")
                all_articles.extend(results)
            except Exception as e:
                log_error(f"Falha na busca em {source}", e)
    
    return all_articles

# --- FUNÇÕES AUXILIARES ---

def get_drive_service():
    """Configuração robusta do serviço do Google Drive"""
    creds = None
    
    try:
        if GOOGLE_TOKEN_JSON and GOOGLE_CREDENTIALS_JSON:
            log_info("Executando em modo PRODUÇÃO")
            try:
                creds_json = json.loads(GOOGLE_TOKEN_JSON)
                creds = Credentials.from_authorized_user_info(creds_json, SCOPES)
                log_info("Credenciais carregadas das variáveis de ambiente")
            except json.JSONDecodeError as e:
                log_error("Erro ao decodificar GOOGLE_TOKEN_JSON", e)
                raise Exception("A variável de ambiente GOOGLE_TOKEN_JSON não é um JSON válido.")
        
        elif os.path.exists(TOKEN_FILE):
            log_info("Executando em modo DESENVOLVIMENTO")
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        else:
            log_error("Nenhuma credencial encontrada")
            raise Exception("Credenciais do Google não encontradas.")

        if not creds or not creds.valid:
            log_info("Credenciais inválidas, tentando renovar")
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    log_info("Token renovado com sucesso")
                    
                    if not GOOGLE_TOKEN_JSON and os.path.exists('credentials.json'):
                        with open(TOKEN_FILE, 'w') as token:
                            token.write(creds.to_json())
                            log_info("Novo token salvo localmente")
                            
                except Exception as e:
                    log_error("Falha ao renovar token", e)
                    raise Exception(f"Token expirado: {e}")
            else:
                if not GOOGLE_TOKEN_JSON:
                    if not os.path.exists('credentials.json'):
                        raise Exception("Arquivo 'credentials.json' não encontrado.")
                    log_info("Iniciando fluxo de autorização local")
                    flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                    creds = flow.run_local_server(port=0)
                    with open(TOKEN_FILE, 'w') as token:
                        token.write(creds.to_json())
                else:
                    raise Exception("Token inválido em produção.")
        
        service = build('drive', 'v3', credentials=creds)
        log_info("Serviço do Google Drive configurado")
        return service
        
    except Exception as e:
        log_error("Erro na configuração do Google Drive", e)
        raise

def get_ai_search_strategies(research_question, api_key):
    """Gera estratégias de busca usando IA"""
    if not api_key:
        log_info("API key do Gemini não disponível")
        return [{'query': research_question, 'rationale': 'Busca direta.', 'topic': 'Busca Direta'}]
    
    try:
        log_info(f"Gerando estratégias de busca para: {research_question}")
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        prompt = f"""
        Aja como um assistente de pesquisa sênior. Baseado na pergunta em português: "{research_question}", gere 3-5 estratégias de busca em inglês otimizadas para múltiplas bases acadêmicas.
        
        Retorne **APENAS** um JSON válido com uma lista de objetos. Cada objeto deve ter:
        - "query": a string de busca em inglês (ex: "user experience adoption SME")
        - "rationale": uma justificativa concisa em português sobre o foco da busca
        - "topic": um tópico categórico e curto em português (máximo 3 palavras, ex: "Adoção UX")
        
        Exemplo de retorno:
        [
            {{"query": "user experience adoption small medium enterprises", "rationale": "Busca geral sobre adoção de UX em PMEs", "topic": "Adoção UX"}},
            {{"query": "UX implementation challenges SME", "rationale": "Foco nos desafios de implementação", "topic": "Desafios"}},
            {{"query": "human computer interaction business", "rationale": "Perspectiva de HCI no contexto empresarial", "topic": "HCI Empresarial"}}
        ]
        """
        
        response = model.generate_content(prompt)
        cleaned_response = response.text.strip().replace('```json', '').replace('```', '')
        
        decoded_data = json.loads(cleaned_response)
        if not isinstance(decoded_data, list):
            log_error("Resposta da IA não é uma lista válida")
            return [{'query': research_question, 'rationale': 'Falha na IA.', 'topic': 'Busca Direta'}]
        
        log_info(f"Geradas {len(decoded_data)} estratégias de busca")
        return decoded_data
        
    except json.JSONDecodeError as e:
        log_error("Erro ao decodificar resposta da IA", e)
        return [{'query': research_question, 'rationale': 'Falha na IA.', 'topic': 'Busca Direta'}]
    except Exception as e:
        log_error("Erro geral na geração de estratégias", e)
        return [{'query': research_question, 'rationale': 'Falha na IA.', 'topic': 'Busca Direta'}]

def get_ai_summary(abstract, api_key):
    """Gera resumo analítico usando IA"""
    if not abstract or "resumo não disponível" in abstract.lower():
        return "Não foi possível gerar o resumo."
    
    if not api_key:
        return "API key do Gemini não disponível."
    
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
        time.sleep(1)
        return response.text.strip()
        
    except Exception as e:
        log_error("Erro ao gerar resumo analítico", e)
        return "Ocorreu um erro ao tentar gerar o resumo analítico."

def deduplicate_articles(articles, saved_articles_ids=None):
    """Remove artigos duplicados de múltiplas fontes"""
    if saved_articles_ids is None:
        saved_articles_ids = set()
    
    seen_titles = set()
    seen_urls = set()
    seen_ids = set()
    unique_articles = []
    
    for article in articles:
        if article.get('id') in saved_articles_ids:
            continue
        
        title = article.get('title', '').lower().strip()
        url = article.get('url', '').strip()
        article_id = article.get('id', '').strip()
        
        is_duplicate = False
        for seen_title in seen_titles:
            if title and seen_title and (
                title == seen_title or 
                (len(title) > 20 and len(seen_title) > 20 and title in seen_title) or
                (len(title) > 20 and len(seen_title) > 20 and seen_title in title)
            ):
                is_duplicate = True
                break
        
        if not is_duplicate and (
            (url and url in seen_urls) or 
            (article_id and article_id in seen_ids)
        ):
            is_duplicate = True
        
        if not is_duplicate and title and url:
            unique_articles.append(article)
            seen_titles.add(title)
            if url:
                seen_urls.add(url)
            if article_id:
                seen_ids.add(article_id)
    
    return unique_articles

def sanitize_filename(text):
    if not text:
        return "Sem_Nome"
    return re.sub(r'[\\/*?:"<>|]', "", text).strip()

def get_or_create_folder(service, folder_name):
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
    try:
        query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
        response = service.files().list(q=query, fields='files(id)').execute()
        
        files = response.get('files', [])
        if files:
            request = service.files().get_media(fileId=files[0]['id'])
            content = io.BytesIO(request.execute()).read().decode('utf-8')
            log_info(f"Arquivo '{filename}' baixado")
            return content
        return None
    except Exception as e:
        log_error(f"Erro ao baixar arquivo '{filename}'", e)
        return None

def upload_text_file(service, folder_id, filename, content):
    try:
        file_metadata = {'name': filename, 'parents': [folder_id]}
        media = io.BytesIO(content.encode('utf-8'))
        media_body = MediaIoBaseUpload(media, mimetype='text/plain', resumable=True)
        
        query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
        response = service.files().list(q=query, fields='files(id)').execute()
        
        files = response.get('files', [])
        if files:
            service.files().update(fileId=files[0]['id'], media_body=media_body).execute()
            log_info(f"Arquivo '{filename}' atualizado")
        else:
            service.files().create(body=file_metadata, media_body=media_body).execute()
            log_info(f"Arquivo '{filename}' criado")
    except Exception as e:
        log_error(f"Erro ao fazer upload do arquivo '{filename}'", e)
        raise

def load_saved_articles_from_drive(service, folder_id):
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
    try:
        content = json.dumps(articles, indent=2, ensure_ascii=False)
        upload_text_file(service, folder_id, SAVED_ARTICLES_FILENAME, content)
        log_info(f"Salvos {len(articles)} artigos no Drive")
    except Exception as e:
        log_error("Erro ao salvar artigos no Drive", e)
        raise

def format_abnt(article):
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

def scrape_researchgate_metadata(url):
    """Extrai metadados do ResearchGate"""
    try:
        log_info(f"Extraindo metadados do ResearchGate: {url}")
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
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

# --- ROTAS DA API ---

@app.route('/api/search', methods=['POST'])
def handle_search():
    """Rota para busca de artigos em múltiplas fontes"""
    try:
        log_info("Iniciando busca de artigos em múltiplas fontes")
        data = request.json
        
        if not data:
            return jsonify({"error": "Dados não fornecidos"}), 400
        
        query_text = data.get('queryText', '').strip()
        if not query_text:
            return jsonify({"error": "Texto de busca é obrigatório"}), 400
        
        min_year = int(data.get('minYear', 2020))
        min_citations = int(data.get('minCitations', 10))
        search_type = data.get('searchType', 'direct')
        selected_sources = data.get('sources', None)
        
        log_info(f"Parâmetros: query='{query_text}', type='{search_type}', min_year={min_year}, min_citations={min_citations}")
        
        if search_type == 'ia':
            strategies = get_ai_search_strategies(query_text, GEMINI_API_KEY)
        else:
            strategies = [{'query': query_text, 'rationale': 'Busca direta.', 'topic': 'Busca Direta'}]
        
        if not strategies:
            return jsonify({"error": "Não foi possível gerar estratégias de busca."}), 500
        
        try:
            service = get_drive_service()
            folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
            saved_articles = load_saved_articles_from_drive(service, folder_id)
            saved_ids = {a.get('id') for a in saved_articles if a.get('id')}
        except Exception as e:
            log_error("Erro na configuração do Google Drive", e)
            return jsonify({"error": f"Erro na configuração do Google Drive: {str(e)}"}), 500
        
        all_found_articles = []
        
        for strategy in strategies:
            query = strategy.get('query', '').strip()
            if not query:
                continue
                
            rationale = strategy.get('rationale', 'Busca')
            topic = strategy.get('topic', 'Geral')
            
            log_info(f"Executando busca: {query} (Tópico: {topic})")
            
            strategy_articles = search_all_sources(query, min_year, min_citations, selected_sources)
            
            for article in strategy_articles:
                article['topic'] = rationale
                article['search_strategy'] = topic
            
            all_found_articles.extend(strategy_articles)
        
        unique_articles = deduplicate_articles(all_found_articles, saved_ids)
        
        current_year = datetime.datetime.now().year
        for article in unique_articles:
            year = article.get('year', current_year)
            citations = article.get('citations', 0)
            age = current_year - year
            recency_factor = max(0.5, 1.0 - (age * 0.05))
            article['relevance_score'] = citations * recency_factor
        
        unique_articles.sort(key=lambda x: x.get('relevance_score', 0), reverse=True)
        
        log_info(f"Busca concluída: {len(unique_articles)} artigos únicos encontrados de {len(all_found_articles)} totais")
        
        source_stats = {}
        for article in all_found_articles:
            source = article.get('source', 'Desconhecida').split('(')[0].strip()
            source_stats[source] = source_stats.get(source, 0) + 1
        
        return jsonify({
            'articles': unique_articles,
            'total_found': len(all_found_articles),
            'unique_count': len(unique_articles),
            'source_stats': source_stats,
            'strategies_used': len(strategies)
        })
        
    except Exception as e:
        log_error("Erro geral na busca", e)
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

@app.route('/api/sources', methods=['GET'])
def handle_get_sources():
    """Retorna lista de fontes disponíveis e seu status"""
    sources = {
        'semantic_scholar': {
            'name': 'Semantic Scholar',
            'description': 'Base científica da Allen Institute',
            'status': 'active',
            'requires_key': False
        },
        'crossref': {
            'name': 'CrossRef',
            'description': 'Metadados de publicações científicas',
            'status': 'active',
            'requires_key': False
        },
        'web_of_science': {
            'name': 'Web of Science',
            'description': 'Base premium da Clarivate',
            'status': 'active' if WOS_API_KEY else 'needs_key',
            'requires_key': True
        },
        'doaj': {
            'name': 'DOAJ',
            'description': 'Directory of Open Access Journals',
            'status': 'active',
            'requires_key': False
        },
        'arxiv': {
            'name': 'arXiv',
            'description': 'Repositório de preprints',
            'status': 'active',
            'requires_key': False
        },
        'openalex': {
            'name': 'OpenAlex',
            'description': 'Base acadêmica aberta',
            'status': 'active',
            'requires_key': False
        },
        'pubmed': {
            'name': 'PubMed',
            'description': 'Base biomédica do NCBI',
            'status': 'active',
            'requires_key': False
        },
        'core': {
            'name': 'CORE',
            'description': 'Agregador de repositórios abertos',
            'status': 'active' if CORE_API_KEY else 'needs_key',
            'requires_key': True
        }
    }
    
    return jsonify(sources)

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
        
        content = file.read().decode('utf-8')
        bib_database = bibtexparser.loads(content)
        
        articles = []
        for entry in bib_database.entries:
            try:
                authors = []
                if entry.get('author'):
                    authors = [name.strip() for name in entry.get('author', '').split(' and ')]
                
                article = {
                    'id': f"bib_{entry.get('doi') or entry.get('ID', '')}",
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
        
        if "researchgate.net" in url:
            article_data = scrape_researchgate_metadata(url)
        else:
            return jsonify({"error": "Atualmente, apenas links do ResearchGate são suportados."}), 400
        
        if not article_data:
            return jsonify({"error": "Não foi possível extrair os dados da URL."}), 500
        
        service = get_drive_service()
        folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
        
        today = datetime.date.today().strftime("%Y-%m-%d")
        saved_articles = load_saved_articles_from_drive(service, folder_id)
        saved_ids = {a.get('id') for a in saved_articles if a.get('id')}
        
        if article_data['id'] in saved_ids:
            return jsonify({"status": "info", "message": "Este artigo já existe no seu fichamento."})
        
        ai_summary = get_ai_summary(article_data.get('abstract'), GEMINI_API_KEY)
        
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
        
        upload_text_file(service, folder_id, filename, file_content)
        
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
        
        service = get_drive_service()
        folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
        
        today = datetime.date.today().strftime("%Y-%m-%d")
        saved_articles = load_saved_articles_from_drive(service, folder_id)
        saved_ids = {a.get('id') for a in saved_articles if a.get('id')}
        
        articles_processed = 0
        
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
                    
                    author_part = sanitize_filename("Autor")
                    if article.get('authors') and len(article['authors']) > 0:
                        author_name = article['authors'][0]
                        if ' ' in author_name:
                            author_part = sanitize_filename(author_name.split(' ')[-1])
                        else:
                            author_part = sanitize_filename(author_name)
                    
                    title_part = sanitize_filename(article.get('title', 'Sem_Titulo'))[:30]
                    filename = f"{author_part}_{article.get('year', 'SD')}_{title_part}.md"
                    
                    file_content = f"""# {article.get('title', 'N/A')}

**Informações Bibliográficas:**
- **Autores:** {', '.join(article.get('authors', []))}
- **Ano:** {article.get('year', 'N/A')}
- **Citações:** {article.get('citations', 'N/A')}
- **Tópico:** {article.get('topic', 'N/A')}
- **Fonte:** {article.get('source', 'N/A')}
- **Venue:** {article.get('venue', 'N/A')}
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
                    
                    upload_text_file(service, folder_id, filename, file_content)
                    
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
        
        relevant_articles = [art for art in saved_articles if art.get('read') and art.get('specificObjective')]
        
        framework = defaultdict(list)
        for article in relevant_articles:
            objective = article['specificObjective']
            framework[objective].append(format_abnt(article))
        
        log_info(f"Referencial construído com {len(relevant_articles)} artigos em {len(framework)} objetivos")
        return jsonify(framework), 200
        
    except Exception as e:
        log_error("Erro ao construir referencial teórico", e)
        return jsonify({"error": str(e)}), 500

@app.route('/')
def serve_index():
    """Serve a página principal"""
    return send_from_directory('static', 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    """Serve arquivos estáticos"""
    return send_from_directory('static', path)

@app.route('/health')
def health_check():
    """Endpoint de saúde para o Render"""
    status = {
        "status": "healthy",
        "message": "Agente de Pesquisa funcionando",
        "apis": {
            "gemini": "configured" if GEMINI_API_KEY else "missing",
            "web_of_science": "configured" if WOS_API_KEY else "missing",
            "core": "configured" if CORE_API_KEY else "missing",
            "google_drive": "configured" if GOOGLE_TOKEN_JSON else "local_only"
        },
        "sources_available": [
            "semantic_scholar", "crossref", "doaj", "arxiv", 
            "openalex", "pubmed"
        ]
    }
    
    if WOS_API_KEY:
        status["sources_available"].append("web_of_science")
    
    if CORE_API_KEY:
        status["sources_available"].append("core")
    
    return jsonify(status), 200

# Executa validação na inicialização
validate_config()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV') != 'production'
    
    log_info(f"Iniciando servidor na porta {port}")
    log_info(f"Modo debug: {debug}")
    log_info(f"Gemini API configurada: {'Sim' if GEMINI_API_KEY else 'Não'}")
    log_info(f"Google Drive em produção: {'Sim' if GOOGLE_TOKEN_JSON else 'Não'}")
    log_info(f"Web of Science API: {'Sim' if WOS_API_KEY else 'Não'}")
    log_info(f"CORE API: {'Sim' if CORE_API_KEY else 'Não'}")
    
    app.run(host='0.0.0.0', port=port, debug=debug)
