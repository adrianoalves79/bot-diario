import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import pdfplumber
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

URL = "https://municipioonline.com.br/se/prefeitura/simaodias/cidadao/diariooficial"
ARQUIVO_ESTADO = "ultimo_diario.txt"
ARQUIVO_CONTROLE_DIA = "controle_dia.txt"
PASTA_DOWNLOAD = Path("downloads")
PASTA_DOWNLOAD.mkdir(exist_ok=True)

# ========= TELEGRAM =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
# ===========================

# ========= TESTE =========
PDF_TESTE = "https://municipioonline.com.br/se/prefeitura/simaodias/cidadao/diariooficial/diario?n=diario.pdf&l=1ui-eDtGgoKt2fkb4jn-TkTBln90DcyRT"
# =========================


def enviar_telegram(mensagem):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠️ Telegram não configurado.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": mensagem
    }

    try:
        r = requests.post(url, data=data, timeout=30)
        if r.status_code == 200:
            print("📨 Alerta enviado ao Telegram.")
        else:
            print(f"⚠️ Falha ao enviar Telegram: {r.status_code} - {r.text}")
    except Exception as e:
        print("⚠️ Erro ao enviar Telegram:", e)


def obter_edicao_atual():
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(URL, headers=headers, timeout=30)
    html = r.text

    m_edicao = re.search(r"Diário Oficial Nº\s*([0-9]+/[0-9]+)", html, re.IGNORECASE)
    m_data = re.search(r"(\d{2}/\d{2}/\d{4})", html)

    if not m_edicao or not m_data:
        return None, None

    return m_edicao.group(1).strip(), m_data.group(1).strip()


def criar_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--log-level=3")
    options.page_load_strategy = "eager"
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(60)
    return driver


def extrair_pdf_apos_clicar():
    driver = criar_driver()
    try:
        print("🌐 Abrindo site...")
        driver.get(URL)

        wait = WebDriverWait(driver, 30)

        print("⏳ Aguardando botão Download...")
        botao = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//*[contains(normalize-space(.), 'Download')]")
            )
        )

        try:
            driver.get_log("performance")
        except Exception:
            pass

        print("📥 Clicando em Download...")
        driver.execute_script("arguments[0].click();", botao)

        time.sleep(6)

        pdf_link = None
        logs = driver.get_log("performance")

        for entry in logs:
            try:
                msg = json.loads(entry["message"])["message"]
                method = msg.get("method", "")

                if method not in ("Network.requestWillBeSent", "Network.responseReceived"):
                    continue

                params = msg.get("params", {})

                req = params.get("request", {})
                req_url = req.get("url", "")
                if "diario?n=diario.pdf" in req_url:
                    pdf_link = req_url

                resp = params.get("response", {})
                resp_url = resp.get("url", "")
                if "diario?n=diario.pdf" in resp_url:
                    pdf_link = resp_url

            except Exception:
                continue

        if not pdf_link:
            raise RuntimeError("Não foi possível capturar a URL real do PDF após clicar em Download.")

        pdf_link = pdf_link.replace("&amp;", "&").split("#")[0]

        print("📄 URL real do PDF:")
        print(pdf_link)

        return pdf_link

    finally:
        driver.quit()


def baixar_pdf(pdf_link):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": URL,
    }

    r = requests.get(pdf_link, headers=headers, timeout=60)

    if r.status_code != 200:
        raise RuntimeError(f"Falha ao baixar PDF. Status: {r.status_code}")

    if not r.content.startswith(b"%PDF"):
        raise RuntimeError("O arquivo recebido não é um PDF válido.")

    pdf_path = PASTA_DOWNLOAD / "diario_atual.pdf"
    with open(pdf_path, "wb") as f:
        f.write(r.content)

    return pdf_path


def limpar_espacos(texto):
    return re.sub(r"\s+", " ", str(texto or "")).strip()


def extrair_candidato_de_linha(linha_texto):
    linha_texto = limpar_espacos(linha_texto)

    if not linha_texto:
        return None

    if "NOME DO CANDIDATO" in linha_texto.upper():
        return None

    padrao = re.compile(
        r"^(?P<inscricao>\d+P\d+)\s+"
        r"(?P<nome>.+?)\s+"
        r"(?P<cpf>\d[\d\*\.]*?)\s+"
        r"(?P<nascimento>\d{2}/\d{2}/\d{4})\s+"
        r"(?P<nota>\d{1,2})\s+"
        r"(?P<classificacao>\d+)\s*$"
    )

    m = padrao.match(linha_texto)
    if not m:
        return None

    return {
        "inscricao": limpar_espacos(m.group("inscricao")),
        "nome": limpar_espacos(m.group("nome")),
        "nascimento": limpar_espacos(m.group("nascimento")),
        "nota": limpar_espacos(m.group("nota")),
        "classificacao": limpar_espacos(m.group("classificacao")),
    }


def analisar_porteiro(pdf_path):
    print("📖 Lendo PDF...")

    candidatos = []
    dentro_porteiro = False

    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            texto = pagina.extract_text() or ""
            linhas = texto.splitlines()

            for linha in linhas:
                linha_limpa = limpar_espacos(linha)
                linha_upper = linha_limpa.upper()

                if "ÁREA: PORTEIRO" in linha_upper or "AREA: PORTEIRO" in linha_upper:
                    dentro_porteiro = True
                    continue

                if dentro_porteiro and (
                    ("ÁREA:" in linha_upper or "AREA:" in linha_upper)
                    and "PORTEIRO" not in linha_upper
                ):
                    dentro_porteiro = False
                    continue

                if not dentro_porteiro:
                    continue

                candidato = extrair_candidato_de_linha(linha_limpa)
                if candidato:
                    candidatos.append(candidato)

    unicos = []
    vistos = set()

    for c in candidatos:
        chave = (c["inscricao"], c["nome"], c["classificacao"])
        if chave not in vistos:
            vistos.add(chave)
            unicos.append(c)

    return unicos


def montar_mensagem_sem_convocacao(data, edicao):
    return (
        "🚨 Novo Diário Oficial detectado\n\n"
        f"📅 Data: {data}\n"
        f"📄 Edição: {edicao}\n\n"
        "⚠️ Não houve convocação para o cargo de PORTEIRO neste diário."
    )


def montar_mensagem_com_convocacao(candidatos, data, edicao):
    mensagem = (
        "🚨 NOVO DIÁRIO DETECTADO!\n"
        "✅ Convocação para PORTEIRO encontrada\n\n"
        f"📅 Data: {data}\n"
        f"📄 Edição: {edicao}\n\n"
    )

    for i, c in enumerate(candidatos, start=1):
        mensagem += (
            f"{i})\n"
            f"INSCRIÇÃO: {c['inscricao']}\n"
            f"NOME: {c['nome']}\n"
            f"NOTA DE TÍTULOS: {c['nota']}\n"
            f"CLASSIFICAÇÃO: {c['classificacao']}\n\n"
        )

    mensagem += (
        "📊 RESUMO PORTEIRO\n"
        f"Total convocados: {len(candidatos)}\n"
        f"Última classificação chamada: {candidatos[-1]['classificacao']}"
    )

    return mensagem


def ja_processou_hoje():
    if not os.path.exists(ARQUIVO_CONTROLE_DIA):
        return False

    with open(ARQUIVO_CONTROLE_DIA, "r", encoding="utf-8") as f:
        data = f.read().strip()

    hoje = datetime.now().strftime("%d/%m/%Y")
    return data == hoje


def marcar_processado_hoje():
    hoje = datetime.now().strftime("%d/%m/%Y")
    with open(ARQUIVO_CONTROLE_DIA, "w", encoding="utf-8") as f:
        f.write(hoje)


def verificar_diario():
    print("\n🔎 Verificando Diário Oficial...")

    if PDF_TESTE:
        print("🧪 MODO TESTE ATIVADO")

        pdf_path = baixar_pdf(PDF_TESTE)
        candidatos = analisar_porteiro(pdf_path)

        if not candidatos:
            mensagem = montar_mensagem_sem_convocacao("11/03/2026", "TESTE")
        else:
            mensagem = montar_mensagem_com_convocacao(candidatos, "11/03/2026", "TESTE")

        enviar_telegram(mensagem)
        return True

    if ja_processou_hoje():
        print("✅ Já processado hoje. Encerrando execução.")
        return False

    edicao, data = obter_edicao_atual()

    if not edicao or not data:
        print("❌ Não foi possível identificar a edição atual no site.")
        return False

    atual = f"{data}|{edicao}"
    print("📅 Data atual:", data)
    print("📄 Edição atual:", edicao)

    anterior = ""
    if os.path.exists(ARQUIVO_ESTADO):
        with open(ARQUIVO_ESTADO, "r", encoding="utf-8") as f:
            anterior = f.read().strip()

    if atual == anterior:
        print("📄 Nenhum diário novo.")
        return False

    print("🚨 NOVO DIÁRIO DETECTADO!")

    pdf_link = extrair_pdf_apos_clicar()
    pdf_path = baixar_pdf(pdf_link)

    print("✅ PDF baixado:", pdf_path)

    with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
        f.write(atual)

    candidatos = analisar_porteiro(pdf_path)

    print("\n📊 RESULTADO")

    if not candidatos:
        print("⚠️ Não houve convocação para o cargo de PORTEIRO neste diário.")
        mensagem = montar_mensagem_sem_convocacao(data, edicao)
        enviar_telegram(mensagem)
        marcar_processado_hoje()
        return True

    print("\n🚪 CANDIDATOS - PORTEIRO\n")
    for c in candidatos:
        print("================================")
        print("Nº DE INSCRIÇÃO:", c["inscricao"])
        print("NOME:", c["nome"])
        print("DATA DE NASCIMENTO:", c["nascimento"])
        print("NOTA DE TÍTULOS:", c["nota"])
        print("CLASSIFICAÇÃO:", c["classificacao"])

    print("\n📊 RESUMO PORTEIRO")
    print("----------------------------")
    print("Total convocados:", len(candidatos))
    print("Última classificação chamada:", candidatos[-1]["classificacao"])

    mensagem = montar_mensagem_com_convocacao(candidatos, data, edicao)
    enviar_telegram(mensagem)
    marcar_processado_hoje()
    return True


if __name__ == "__main__":
    try:
        verificar_diario()
    except Exception as e:
        print("❌ Erro:", e)
        raise
