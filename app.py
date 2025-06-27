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
# A pasta 'static' agora é servida pelo Flask
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
TOKEN_FILE = 'token.json'

# --- FUNÇÕES COMPLETAS DO AGENTE ---

def get_drive_service():
    creds = None
    if GOOGLE_TOKEN_JSON:
        creds_json = json.loads(GOOGLE_TOKEN_JSON)
        creds = Credentials.from_authorized_user_info(creds_json, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Se o token for atualizado, precisamos de uma forma de o salvar de volta no ambiente do Render
            # Esta é uma limitação do fluxo; o ideal é gerar um novo token.json e atualizar a variável de ambiente.
            print("Aviso: O token de acesso foi atualizado. Pode ser necessário atualizar a variável de ambiente GOOGLE_TOKEN_JSON no futuro.")
        else:
            raise Exception("O token de acesso expirou e não pôde ser atualizado. Por favor, gere um novo token.json localmente e atualize a variável de ambiente GOOGLE_TOKEN_JSON no Render.")

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

# ... (todas as outras funções permanecem aqui, sem alterações)

# --- ROTAS DA API ---
# ... (todas as rotas @app.route para /api/... permanecem aqui)

# --- NOVA ROTA PARA SERVIR A INTERFACE ---
@app.route('/')
def serve_index():
    return send_from_directory('static', 'index.html')

# --- INICIA O SERVIDOR (MODIFICADO PARA DEPLOY) ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Iniciando servidor na porta {port}...")
    app.run(host='0.0.0.0', port=port)
