import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber
import requests
from requests.adapters import HTTPAdapter
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from urllib3.util.retry import Retry
from webdriver_manager.chrome import ChromeDriverManager

URL = "https://municipioonline.com.br/se/prefeitura/simaodias/cidadao/diariooficial"
ARQUIVO_ESTADO = "ultimo_diario.txt"
PASTA_DOWNLOAD = Path("downloads")
PASTA_DOWNLOAD.mkdir(exist_ok=True)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# ============= TESTE MANUAL ============================
PDF_TESTE = ""
# Exemplo:
# PDF_TESTE = "https://site.com/arquivo.pdf"
# ======================================================


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


def criar_sessao_http():
    sess = requests.Session()

    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"]
    )

    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)

    sess.headers.update({
        "User-Agent": "Mozilla/5.0"
    })

    return sess


def extrair_edicao_data_do_html(html):
    m_edicao = re.search(r"Diário Oficial Nº\s*([0-9]+/[0-9]+)", html, re.IGNORECASE)
    m_data = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", html)

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

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(60)
    return driver


def obter_edicao_atual():
    try:
        sess = criar_sessao_http()
        r = sess.get(URL, timeout=(20, 90))
        r.raise_for_status()

        edicao, data = extrair_edicao_data_do_html(r.text)
        if edicao and data:
            return edicao, data

        print("⚠️ Requests abriu a página, mas não encontrou edição/data. Tentando via Selenium...")
    except Exception as e:
        print(f"⚠️ Falha no requests para obter edição atual: {e}")
        print("🔁 Tentando obter edição atual via Selenium...")

    driver = criar_driver()
    try:
        driver.get(URL)
        wait = WebDriverWait(driver, 45)
        wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//*[contains(., 'Diário Oficial Nº')]")
            )
        )

        html = driver.page_source
        edicao, data = extrair_edicao_data_do_html(html)
        if edicao and data:
            return edicao, data

        raise RuntimeError("Página carregou, mas não foi possível extrair edição/data.")
    finally:
        driver.quit()


def baixar_pdf(url_pdf):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": URL,
        "Accept": "application/pdf,application/octet-stream,*/*",
    }

    sess = criar_sessao_http()
    r = sess.get(url_pdf, headers=headers, timeout=(20, 120), allow_redirects=True)

    if r.status_code != 200:
        raise RuntimeError(f"Falha ao baixar PDF. Status: {r.status_code}")

    content_type = (r.headers.get("Content-Type") or "").lower()

    if not r.content.startswith(b"%PDF") and "pdf" not in content_type:
        raise RuntimeError(
            f"O arquivo recebido não parece ser um PDF válido. Content-Type: {content_type}"
        )

    pdf_path = PASTA_DOWNLOAD / "diario_atual.pdf"
    with open(pdf_path, "wb") as f:
        f.write(r.content)

    return pdf_path


def localizar_pdf_na_pagina(driver):
    print("🌐 Abrindo site...")
    driver.get(URL)

    wait = WebDriverWait(driver, 30)
    wait.until(
        EC.presence_of_element_located(
            (By.XPATH, "//*[contains(., 'Diário Oficial Nº')]")
        )
    )

    time.sleep(3)

    candidatos = []

    seletores = [
        "iframe",
        "embed",
        "object",
        "a[href]",
        "source"
    ]

    for seletor in seletores:
        elementos = driver.find_elements(By.CSS_SELECTOR, seletor)
        for el in elementos:
            try:
                valor = (
                    el.get_attribute("src")
                    or el.get_attribute("data")
                    or el.get_attribute("href")
                )

                if not valor:
                    continue

                valor = valor.strip()
                if not valor:
                    continue

                url_abs = urljoin(URL, valor)

                texto_ref = " ".join([
                    (el.get_attribute("outerHTML") or "")[:300],
                    valor
                ]).lower()

                if (
                    ".pdf" in texto_ref
                    or "diario" in texto_ref
                    or "viewer" in texto_ref
                    or "arquivo" in texto_ref
                    or "download" in texto_ref
                ):
                    candidatos.append(url_abs)
            except Exception:
                continue

    try:
        html = driver.page_source
        encontrados = re.findall(
            r'https?://[^"\']+',
            html,
            flags=re.IGNORECASE
        )
        for url in encontrados:
            url_lower = url.lower()
            if (
                ".pdf" in url_lower
                or "diario" in url_lower
                or "viewer" in url_lower
                or "download" in url_lower
            ):
                candidatos.append(url)
    except Exception:
        pass

    vistos = set()
    unicos = []
    for c in candidatos:
        c_limpo = c.split("#")[0].strip()
        if c_limpo not in vistos:
            vistos.add(c_limpo)
            unicos.append(c_limpo)

    unicos.sort(key=lambda x: (".pdf" not in x.lower(), len(x)))

    print("🔍 Candidatos encontrados na página:")
    for c in unicos:
        print(" -", c)

    if not unicos:
        raise RuntimeError("Não encontrei iframe/embed/object/link com o arquivo do diário.")

    return unicos


def baixar_primeiro_pdf_valido(driver, candidatos):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": URL,
        "Accept": "application/pdf,application/octet-stream,*/*",
    }

    sess = criar_sessao_http()

    for cookie in driver.get_cookies():
        sess.cookies.set(cookie["name"], cookie["value"])

    ultimo_erro = None

    for link in candidatos:
        try:
            link = link.split("#")[0].strip()

            print(f"📥 Tentando baixar: {link}")
            r = sess.get(link, headers=headers, timeout=(20, 120), allow_redirects=True)

            content_type = (r.headers.get("Content-Type") or "").lower()

            if r.status_code == 200 and (
                r.content.startswith(b"%PDF") or "pdf" in content_type
            ):
                pdf_path = PASTA_DOWNLOAD / "diario_atual.pdf"
                with open(pdf_path, "wb") as f:
                    f.write(r.content)

                print(f"✅ PDF válido encontrado: {link}")
                return pdf_path

            print(f"⚠️ Não era PDF: status={r.status_code} content-type={content_type}")

        except Exception as e:
            ultimo_erro = e
            print(f"⚠️ Falha ao tentar {link}: {e}")

    raise RuntimeError(f"Nenhum candidato resultou em PDF válido. Último erro: {ultimo_erro}")


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

    try:
        edicao, data = obter_edicao_atual()
    except Exception as e:
        print(f"❌ Não foi possível obter a edição atual: {e}")
        return False

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
        candidatos = localizar_pdf_na_pagina(driver)
        pdf_path = baixar_primeiro_pdf_valido(driver, candidatos)
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
