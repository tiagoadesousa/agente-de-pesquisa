# --- CONFIGURAÇÕES GLOBAIS SEGURAS ---
# NUNCA coloque API keys diretamente no código!

# ✅ SEGURO - Apenas variáveis de ambiente
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GOOGLE_CREDENTIALS_JSON = os.environ.get('GOOGLE_CREDENTIALS_JSON')
GOOGLE_TOKEN_JSON = os.environ.get('GOOGLE_TOKEN_JSON')

# APIs de Fontes Acadêmicas - TODAS via ambiente
WOS_API_KEY = os.environ.get('WOS_API_KEY')
CORE_API_KEY = os.environ.get('CORE_API_KEY')
OPENALEX_EMAIL = os.environ.get('OPENALEX_EMAIL', 'seu.email@dominio.com')

DRIVE_FOLDER_NAME = "Fichamentos_Mestrado"
SAVED_ARTICLES_FILENAME = "saved_articles.json"
CROSSREF_MAILTO = "seu.email@dominio.com"
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

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
        log_info("Configure as variáveis de ambiente no Render ou execute localmente com credenciais")
    
    # Log de APIs opcionais
    optional_apis = {
        'Web of Science': WOS_API_KEY,
        'CORE': CORE_API_KEY
    }
    
    for api_name, api_key in optional_apis.items():
        status = "✅ Configurada" if api_key else "⚠️ Não configurada (opcional)"
        log_info(f"{api_name}: {status}")

# Executa validação na inicialização
validate_config()
