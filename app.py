# --- INSTALAÇÕES NECESSÁRIAS ---
# O Render usará o ficheiro requirements.txt para instalar tudo isto.
# pip install Flask Flask-Cors gunicorn google-generativeai requests google-api-python-client google-auth-httplib2 google-auth-oauthlib beautifulsoup4 bibtexparser

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
app = Flask(__name__)
CORS(app)

# --- CONFIGURAÇÕES GLOBAIS ---
# Estas variáveis serão agora lidas do ambiente do servidor (mais seguro)
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DRIVE_FOLDER_NAME = "Fichamentos_Mestrado"
SAVED_ARTICLES_FILENAME = "saved_articles.json"
CROSSREF_MAILTO = "tiago.adesousa@gmail.com"
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'
CREDENTIALS_FILE = 'credentials.json'

# --- FUNÇÕES COMPLETAS DO AGENTE ---

def get_drive_service():
    creds = None
    # Prioriza a variável de ambiente para o token, ideal para o deploy
    if 'GOOGLE_TOKEN_JSON' in os.environ:
        creds_json = json.loads(os.environ['GOOGLE_TOKEN_JSON'])
        creds = Credentials.from_authorized_user_info(creds_json, SCOPES)
    elif os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise Exception(f"Arquivo '{CREDENTIALS_FILE}' não encontrado.")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0) 
        # Salva o token localmente para futuras execuções locais
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
    return build('drive', 'v3', credentials=creds)

# (O resto das funções de busca, IA, etc., permanecem as mesmas)
def get_ai_search_strategies(research_question, api_key):
    if not api_key: return [{'query': research_question, 'rationale': 'Busca direta.', 'topic': 'Busca Direta'}]
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = f"""Aja como um assistente de pesquisa sênior... (Prompt completo)..."""
        response = model.generate_content(prompt)
        cleaned_response = response.text.strip().replace('```json', '').replace('```', '')
        return json.loads(cleaned_response)
    except Exception as e: return [{'query': research_question, 'rationale': 'Falha na IA.', 'topic': 'Busca Direta'}]

# ... (todas as outras funções permanecem aqui)

# --- ROTAS DA API ---
# ... (todas as rotas @app.route permanecem aqui)

# --- INICIA O SERVIDOR (MODIFICADO PARA DEPLOY) ---
if __name__ == '__main__':
    # A porta é lida do ambiente, como o Render espera.
    port = int(os.environ.get('PORT', 5000))
    print(f"Iniciando servidor na porta {port}...")
    # 'host='0.0.0.0'' torna o servidor acessível externamente.
    app.run(host='0.0.0.0', port=port)
