import json
import os
import re
import time
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
PASTA_DOWNLOAD = Path("downloads")
PASTA_DOWNLOAD.mkdir(exist_ok=True)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# =============TESTE MANUAL============================
PDF_TESTE = ""
# =====================================================


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


def limpar(texto):
    return re.sub(r"\s+", " ", str(texto or "")).strip()


def somente_digitos(texto):
    return re.sub(r"\D", "", str(texto or ""))


def parece_data(texto):
    return bool(re.fullmatch(r"\d{2}/\d{2}/\d{4}", limpar(texto)))


def parece_inscricao(texto):
    return bool(re.fullmatch(r"\d+P\d+", limpar(texto).upper()))


def normalizar_classificacao(texto):
    return re.sub(r"\D", "", limpar(texto))


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


def extrair_pdf_apos_clicar(driver):
    print("🌐 Abrindo site...")
    driver.get(URL)

    wait = WebDriverWait(driver, 30)

    print("⏳ Aguardando botão Download...")
    botao = wait.until(
        EC.presence_of_element_located(
            (By.XPATH, "//*[contains(normalize-space(.), 'Download')]")
        )
    )

    pdf_link = None

    # 1) Tenta pegar href diretamente do elemento
    try:
        href = botao.get_attribute("href")
        if href and ".pdf" in href.lower():
            pdf_link = href
    except Exception:
        pass

    # 2) Tenta olhar link pai/filho via JS
    if not pdf_link:
        try:
            href = driver.execute_script("""
                const el = arguments[0];
                if (!el) return null;

                if (el.href) return el.href;

                const a1 = el.closest('a');
                if (a1 && a1.href) return a1.href;

                const a2 = el.querySelector && el.querySelector('a');
                if (a2 && a2.href) return a2.href;

                return null;
            """, botao)
            if href and ".pdf" in href.lower():
                pdf_link = href
        except Exception:
            pass

    # 3) Limpa logs antigos antes do clique
    try:
        driver.get_log("performance")
    except Exception:
        pass

    print("📥 Clicando em Download...")
    driver.execute_script("arguments[0].click();", botao)
    time.sleep(8)

    # 4) Captura qualquer URL parecida com PDF nos logs
    if not pdf_link:
        try:
            logs = driver.get_log("performance")
            candidatos = []

            for entry in logs:
                try:
                    msg = json.loads(entry["message"])["message"]
                    method = msg.get("method", "")
                    params = msg.get("params", {})

                    if method == "Network.requestWillBeSent":
                        req_url = params.get("request", {}).get("url", "")
                        if ".pdf" in req_url.lower() or "diario" in req_url.lower():
                            candidatos.append(req_url)

                    elif method == "Network.responseReceived":
                        response = params.get("response", {})
                        resp_url = response.get("url", "")
                        mime = (response.get("mimeType") or "").lower()
                        if ".pdf" in resp_url.lower() or "pdf" in mime:
                            candidatos.append(resp_url)

                except Exception:
                    continue

            for url in candidatos:
                if ".pdf" in url.lower():
                    pdf_link = url
                    break

            if not pdf_link and candidatos:
                pdf_link = candidatos[-1]

        except Exception:
            pass

    # 5) Última tentativa: procurar URL .pdf no HTML
    if not pdf_link:
        try:
            html = driver.page_source
            encontrados = re.findall(
                r'https?://[^"\']+\.pdf[^"\']*',
                html,
                flags=re.IGNORECASE
            )
            if encontrados:
                pdf_link = encontrados[0]
        except Exception:
            pass

    if not pdf_link:
        raise RuntimeError("Não foi possível localizar a URL do PDF após clicar em Download.")

    pdf_link = pdf_link.replace("&amp;", "&").split("#")[0].strip()

    print("📄 URL encontrada do PDF:")
    print(pdf_link)

    return pdf_link


def baixar_pdf(pdf_link):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": URL,
        "Accept": "application/pdf,application/octet-stream,*/*",
    }

    r = requests.get(pdf_link, headers=headers, timeout=60, allow_redirects=True)

    if r.status_code != 200:
        raise RuntimeError(f"Falha ao baixar PDF. Status: {r.status_code}")

    content_type = r.headers.get("Content-Type", "").lower()

    if not r.content.startswith(b"%PDF") and "pdf" not in content_type:
        raise RuntimeError(
            f"O arquivo recebido não parece ser um PDF válido. Content-Type: {content_type}"
        )

    pdf_path = PASTA_DOWNLOAD / "diario_atual.pdf"
    with open(pdf_path, "wb") as f:
        f.write(r.content)

    return pdf_path


def baixar_pdf_com_sessao(driver, pdf_link):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": URL,
        "Accept": "application/pdf,application/octet-stream,*/*",
    }

    sess = requests.Session()

    for cookie in driver.get_cookies():
        sess.cookies.set(cookie["name"], cookie["value"])

    r = sess.get(pdf_link, headers=headers, timeout=60, allow_redirects=True)

    if r.status_code != 200:
        raise RuntimeError(f"Falha ao baixar PDF. Status: {r.status_code}")

    content_type = r.headers.get("Content-Type", "").lower()

    if not r.content.startswith(b"%PDF") and "pdf" not in content_type:
        raise RuntimeError(
            f"O arquivo recebido não parece ser um PDF válido. Content-Type: {content_type}"
        )

    pdf_path = PASTA_DOWNLOAD / "diario_atual.pdf"
    with open(pdf_path, "wb") as f:
        f.write(r.content)

    return pdf_path


def extrair_cargos(pdf_path):
    cargos = set()

    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            texto = pagina.extract_text() or ""
            linhas = texto.splitlines()

            for linha in linhas:
                linha_limpa = limpar(linha)
                linha_upper = linha_limpa.upper()

                if "ÁREA:" in linha_upper or "AREA:" in linha_upper:
                    cargo = re.sub(r"^.*?(ÁREA:|AREA:)\s*", "", linha_upper).strip()
                    if cargo:
                        cargos.add(cargo)

    return sorted(cargos)


def extrair_candidato_de_tabela(linha):
    valores = [limpar(c) for c in linha if limpar(c)]
    if not valores:
        return None

    linha_join = " ".join(valores).upper()

    if "NOME DO CANDIDATO" in linha_join:
        return None

    if not parece_inscricao(valores[0]):
        return None

    if len(valores) >= 5 and parece_data(valores[2]):
        classificacao = normalizar_classificacao(valores[4])
        if not classificacao:
            return None

        return {
            "inscricao": valores[0],
            "nome": valores[1],
            "nascimento": valores[2],
            "nota": somente_digitos(valores[3]) or valores[3],
            "classificacao": classificacao,
        }

    if len(valores) >= 6 and parece_data(valores[3]):
        classificacao = normalizar_classificacao(valores[5])
        if not classificacao:
            return None

        return {
            "inscricao": valores[0],
            "nome": valores[1],
            "nascimento": valores[3],
            "nota": somente_digitos(valores[4]) or valores[4],
            "classificacao": classificacao,
        }

    return None


def extrair_candidato_de_linha(linha_texto):
    linha_texto = limpar(linha_texto)
    if not linha_texto:
        return None

    linha_upper = linha_texto.upper()

    if "NOME DO CANDIDATO" in linha_upper:
        return None
    if "INSCRIÇÃO" in linha_upper or "CLASSIFICAÇÃO" in linha_upper:
        return None

    padrao_sem_cpf = re.compile(
        r"^(?P<inscricao>\d+P\d+)\s+"
        r"(?P<nome>.+?)\s+"
        r"(?P<nascimento>\d{2}/\d{2}/\d{4})\s+"
        r"(?P<nota>\d{1,3})\s+"
        r"(?P<classificacao>\d+º?|\d+o?)\s*$",
        re.IGNORECASE
    )

    m = padrao_sem_cpf.match(linha_texto)
    if m:
        return {
            "inscricao": limpar(m.group("inscricao")),
            "nome": limpar(m.group("nome")),
            "nascimento": limpar(m.group("nascimento")),
            "nota": somente_digitos(m.group("nota")) or limpar(m.group("nota")),
            "classificacao": normalizar_classificacao(m.group("classificacao")),
        }

    padrao_com_cpf = re.compile(
        r"^(?P<inscricao>\d+P\d+)\s+"
        r"(?P<nome>.+?)\s+"
        r"(?P<cpf>\d[\d\*\.]*)\s+"
        r"(?P<nascimento>\d{2}/\d{2}/\d{4})\s+"
        r"(?P<nota>\d{1,3})\s+"
        r"(?P<classificacao>\d+º?|\d+o?)\s*$",
        re.IGNORECASE
    )

    m = padrao_com_cpf.match(linha_texto)
    if m:
        return {
            "inscricao": limpar(m.group("inscricao")),
            "nome": limpar(m.group("nome")),
            "nascimento": limpar(m.group("nascimento")),
            "nota": somente_digitos(m.group("nota")) or limpar(m.group("nota")),
            "classificacao": normalizar_classificacao(m.group("classificacao")),
        }

    return None


def analisar_porteiro(pdf_path):
    print("📖 Lendo PDF...")

    candidatos = []
    dentro_porteiro = False

    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            texto = pagina.extract_text() or ""
            linhas = texto.splitlines()

            for linha in linhas:
                linha_limpa = limpar(linha)
                linha_upper = linha_limpa.upper()

                if "PORTEIRO" in linha_upper:
                    dentro_porteiro = True

                if dentro_porteiro:
                    candidato = extrair_candidato_de_linha(linha_limpa)
                    if candidato:
                        candidatos.append(candidato)

                if dentro_porteiro and (
                    ("ÁREA:" in linha_upper or "AREA:" in linha_upper)
                    and "PORTEIRO" not in linha_upper
                ):
                    dentro_porteiro = False

            if "PORTEIRO" in texto.upper():
                tabelas = pagina.extract_tables() or []
                for tabela in tabelas:
                    for linha in tabela:
                        candidato = extrair_candidato_de_tabela(linha)
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
        "⚠️ Não houve convocação neste diário."
    )


def montar_mensagem_com_cargos(cargos, candidatos_porteiro, data, edicao):
    mensagem = (
        "🚨 NOVO DIÁRIO DETECTADO!\n\n"
        f"📅 Data: {data}\n"
        f"📄 Edição: {edicao}\n\n"
        "📌 Houve convocação para os cargos:\n"
    )

    for cargo in cargos:
        mensagem += f"- {cargo}\n"

    if candidatos_porteiro:
        mensagem += "\n✅ Detalhes de PORTEIRO\n\n"

        for i, c in enumerate(candidatos_porteiro, start=1):
            mensagem += (
                f"{i})\n"
                f"INSCRIÇÃO: {c['inscricao']}\n"
                f"NOME: {c['nome']}\n"
                f"NOTA DE TÍTULOS: {c['nota']}\n"
                f"CLASSIFICAÇÃO: {c['classificacao']}\n\n"
            )

        mensagem += (
            "📊 RESUMO PORTEIRO\n"
            f"Total convocados: {len(candidatos_porteiro)}\n"
            f"Última classificação chamada: {candidatos_porteiro[-1]['classificacao']}"
        )

    return mensagem


def verificar_diario():
    print("\n🔎 Verificando Diário Oficial...")

    if PDF_TESTE:
        print("🧪 MODO TESTE ATIVADO")
        pdf_path = baixar_pdf(PDF_TESTE)
        cargos = extrair_cargos(pdf_path)
        candidatos_porteiro = analisar_porteiro(pdf_path)

        if not cargos:
            mensagem = montar_mensagem_sem_convocacao("TESTE", "TESTE")
        else:
            mensagem = montar_mensagem_com_cargos(cargos, candidatos_porteiro, "TESTE", "TESTE")

        enviar_telegram(mensagem)
        return True

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

    driver = criar_driver()
    try:
        pdf_link = extrair_pdf_apos_clicar(driver)
        pdf_path = baixar_pdf_com_sessao(driver, pdf_link)
    finally:
        driver.quit()

    print("✅ PDF baixado:", pdf_path)

    with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
        f.write(atual)

    cargos = extrair_cargos(pdf_path)
    candidatos_porteiro = analisar_porteiro(pdf_path)

    if not cargos:
        mensagem = montar_mensagem_sem_convocacao(data, edicao)
        enviar_telegram(mensagem)
        return True

    mensagem = montar_mensagem_com_cargos(cargos, candidatos_porteiro, data, edicao)
    enviar_telegram(mensagem)
    return True


if __name__ == "__main__":
    try:
        verificar_diario()
    except Exception as e:
        print("❌ Erro:", e)
        raise
