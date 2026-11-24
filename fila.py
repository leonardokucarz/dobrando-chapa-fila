#!/usr/bin/env python3
"""Fila de publicação do @dobrando_chapa.

Fluxo: o post aprovado entra aqui (vídeo, capa e legenda). O GitHub Pages deixa os
arquivos num link público. A rotina da nuvem roda às 11h58 e às 19h28 e publica
pelo Windsor.ai o que estiver no horário.

Uso no computador (dentro da pasta publicacao/):
  python fila.py add PASTA --quando "2026-11-25 12:00" [--id ID] [--trial SS_PERFORMANCE|MANUAL]
                 [--aprovado] [--forca]
  python fila.py aprova ID [ID ...]
  python fila.py cancela ID
  python fila.py lista
  python fila.py push       # commit + push e espera o GitHub Pages servir os arquivos novos
  python fila.py limpa      # apaga os arquivos dos posts já publicados (o registro fica)
  python fila.py marca ID --media-id X [--permalink URL]    # quando o post saiu à mão

Uso pela rotina da nuvem:
  python3 fila.py devidos                 # JSON com os aprovados no horário (3 h atrás até 20 min à frente)
  python3 fila.py trava ID                # status "publicando"
  python3 fila.py resultado ID --ok --media-id X [--permalink URL] [--obs TEXTO]
  python3 fila.py resultado ID --erro TEXTO

A PASTA do post precisa ter:
  reel.mp4, capa.jpg (ou capa.png) e legenda_post.txt. Opcional: story.jpg ou story.mp4.
  legenda_post.txt = legenda; linha "---"; "COMENTÁRIO PARA FIXAR" + texto; linha "---";
  "STORY" + texto (o story é só sugestão; enquete e quiz não saem pela API).
"""
import argparse
import datetime as dt
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request

AQUI = os.path.dirname(os.path.abspath(__file__))
ARQ = os.path.join(AQUI, "fila.json")
BASE_URL = "https://leonardokucarz.github.io/dobrando-chapa-fila/"
BRT = dt.timezone(dt.timedelta(hours=-3))  # America/Sao_Paulo (sem horário de verão desde 2019)
FEED_MIN_H = 3.0          # posts do feed com menos de 3 h entre eles se atrapalham (dados de 14/11)
DUR_MIN, DUR_MAX = 3, 180  # acima de 3 min o Instagram não recomenda para quem não segue
MAX_MB = 95               # limite do GitHub é 100 MB por arquivo
ATIVOS = ("aguardando_ok", "aprovado", "publicando")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def agora():
    return dt.datetime.now(BRT).replace(microsecond=0)


def carrega():
    with open(ARQ, encoding="utf-8") as f:
        return json.load(f)


def salva(d):
    tmp = ARQ + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, ARQ)


def item(d, pid):
    for it in d["itens"]:
        if it["id"] == pid:
            return it
    sys.exit(f"ERRO: {pid} não está na fila")


def quando_de(txt):
    txt = txt.strip().replace("h", ":")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d/%m/%Y %H:%M"):
        try:
            return dt.datetime.strptime(txt, fmt).replace(tzinfo=BRT)
        except ValueError:
            pass
    try:
        q = dt.datetime.fromisoformat(txt)
        return q if q.tzinfo else q.replace(tzinfo=BRT)
    except ValueError:
        sys.exit(f"ERRO: horário inválido: {txt!r} (use \"AAAA-MM-DD HH:MM\")")


def le_textos(pasta):
    caminho = os.path.join(pasta, "legenda_post.txt")
    if not os.path.exists(caminho):
        sys.exit(f"ERRO: falta {caminho}")
    partes = [p.strip() for p in re.split(r"(?m)^---\s*$", open(caminho, encoding="utf-8").read())]
    legenda = partes[0]
    comentario = story = ""
    for p in partes[1:]:
        cab, _, resto = p.partition("\n")
        if cab.strip().upper().startswith("COMENT"):
            comentario = resto.strip()
        elif cab.strip().upper().startswith("STORY"):
            story = resto.strip()
    return legenda, comentario, story


def sonda(video):
    """Duração, largura e altura pelo ffprobe (só no computador; sem ffprobe, pula)."""
    exe = shutil.which("ffprobe")
    if not exe:
        print("aviso: ffprobe não encontrado, não conferi a duração do vídeo")
        return None
    out = subprocess.run([exe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,codec_name:format=duration", "-of", "json", video],
                         capture_output=True, text=True)
    j = json.loads(out.stdout or "{}")
    st = (j.get("streams") or [{}])[0]
    return {"dur": float(j.get("format", {}).get("duration", 0)), "w": st.get("width"),
            "h": st.get("height"), "codec": st.get("codec_name")}


def capa_jpg(origem, destino):
    if origem.lower().endswith((".jpg", ".jpeg")):
        shutil.copy2(origem, destino)
    else:
        from PIL import Image  # só para converter PNG
        Image.open(origem).convert("RGB").save(destino, "JPEG", quality=90)
    if os.path.getsize(destino) > 8 * 1024 * 1024:
        sys.exit("ERRO: a capa passou de 8 MB (limite do Instagram)")


def cmd_add(a):
    d = carrega()
    pasta = os.path.abspath(a.pasta)
    video = os.path.join(pasta, "reel.mp4")
    if not os.path.exists(video):
        sys.exit(f"ERRO: falta {video}")
    capa = next((os.path.join(pasta, n) for n in ("capa.jpg", "capa.jpeg", "capa.png")
                 if os.path.exists(os.path.join(pasta, n))), None)
    if not capa:
        sys.exit("ERRO: falta capa.jpg (ou capa.png)")
    legenda, comentario, story_txt = le_textos(pasta)
    if len(legenda) > 2200:
        sys.exit(f"ERRO: legenda com {len(legenda)} caracteres (máximo 2.200)")
    tags = re.findall(r"#\w+", legenda)
    if len(tags) > 5:
        sys.exit(f"ERRO: {len(tags)} hashtags (o Instagram aceita 5)")
    mb = os.path.getsize(video) / 1024 / 1024
    if mb > MAX_MB:
        sys.exit(f"ERRO: vídeo com {mb:.0f} MB (máximo {MAX_MB} MB)")
    info = sonda(video)
    if info:
        if not DUR_MIN <= info["dur"] <= DUR_MAX:
            sys.exit(f"ERRO: vídeo com {info['dur']:.1f} s (aceito de {DUR_MIN} a {DUR_MAX} s)")
        if info["w"] and info["h"] and abs(info["w"] / info["h"] - 9 / 16) > 0.02:
            print(f"aviso: vídeo {info['w']}×{info['h']} não é 9:16")
    q = quando_de(a.quando)
    tipo = "trial" if a.trial else "reel"
    pid = a.id or f"{q:%Y-%m-%d_%Hh%M}_{re.sub(r'^[0-9_h-]+', '', os.path.basename(pasta)) or 'post'}"
    if any(it["id"] == pid for it in d["itens"]):
        sys.exit(f"ERRO: já existe {pid} na fila")
    if tipo == "reel" and not a.forca:
        for it in d["itens"]:
            if it["tipo"] == "reel" and it["status"] in ATIVOS + ("publicado",):
                h = abs((quando_de(it["quando"]) - q).total_seconds()) / 3600
                if h < FEED_MIN_H:
                    sys.exit(f"ERRO: {it['id']} está a {h:.1f} h deste horário (mínimo {FEED_MIN_H} h). "
                             "Use --forca se for de propósito.")
    if q < agora():
        sys.exit(f"ERRO: {q:%d/%m %H:%M} já passou")
    rel = f"m/{pid}-{secrets.token_hex(3)}"
    os.makedirs(os.path.join(AQUI, rel), exist_ok=True)
    shutil.copy2(video, os.path.join(AQUI, rel, "reel.mp4"))
    capa_jpg(capa, os.path.join(AQUI, rel, "capa.jpg"))
    story_rel = None
    for n in ("story.jpg", "story.mp4"):
        if os.path.exists(os.path.join(pasta, n)):
            shutil.copy2(os.path.join(pasta, n), os.path.join(AQUI, rel, n))
            story_rel = f"{rel}/{n}"
    d["itens"].append({
        "id": pid, "quando": q.isoformat(), "tipo": tipo,
        "status": "aprovado" if a.aprovado else "aguardando_ok",
        "video": f"{rel}/reel.mp4", "capa": f"{rel}/capa.jpg", "story": story_rel,
        "legenda": legenda, "comentario": comentario, "sugestao_story": story_txt,
        "trial": {"graduation_strategy": a.trial} if a.trial else None,
        "origem": os.path.relpath(pasta, os.path.dirname(AQUI)).replace("\\", "/"),
        "duracao_s": round(info["dur"], 1) if info else None,
        "criado_em": agora().isoformat(), "tentativas": 0,
        "media_id": None, "permalink": None, "publicado_em": None, "erro": None, "obs": None,
    })
    d["itens"].sort(key=lambda it: it["quando"])
    salva(d)
    print(f"ok: {pid} → {q:%a %d/%m %H:%M} ({'aprovado' if a.aprovado else 'aguardando OK'})")


def cmd_aprova(a):
    d = carrega()
    for pid in a.ids:
        it = item(d, pid)
        if it["status"] != "aguardando_ok":
            sys.exit(f"ERRO: {pid} está como {it['status']}")
        it["status"] = "aprovado"
    salva(d)
    print("aprovados:", ", ".join(a.ids))


def cmd_cancela(a):
    d = carrega()
    it = item(d, a.id)
    if it["status"] in ("publicado", "publicando"):
        sys.exit(f"ERRO: {a.id} já está {it['status']}")
    it["status"] = "cancelado"
    salva(d)
    print("cancelado:", a.id)


def cmd_lista(a):
    d = carrega()
    if not d["itens"]:
        print("(fila vazia)")
    for it in d["itens"]:
        q = quando_de(it["quando"])
        extra = it.get("permalink") or it.get("erro") or ""
        print(f"{q:%a %d/%m %H:%M}  {it['status']:<13} {it['tipo']:<5} {it['id']}  {extra}")


def git(*args, ok=False):
    r = subprocess.run(["git", "-C", AQUI, *args], capture_output=True, text=True)
    if r.returncode and not ok:
        sys.exit(f"ERRO git {' '.join(args)}:\n{r.stdout}{r.stderr}")
    return r


def no_ar(url):
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False


def cmd_push(a):
    git("add", "-A")
    if git("diff", "--cached", "--quiet", ok=True).returncode:
        git("commit", "-q", "-m", a.msg or f"fila: atualização {agora():%d/%m %H:%M}")
    git("pull", "-q", "--rebase")
    git("push", "-q")
    d = carrega()
    urls = [BASE_URL + it[k] for it in d["itens"] if it["status"] in ATIVOS
            for k in ("video", "capa", "story") if it.get(k)]
    print(f"push feito; esperando o GitHub Pages servir {len(urls)} arquivo(s)...")
    fim = time.time() + 600
    faltam = urls
    while faltam and time.time() < fim:
        faltam = [u for u in faltam if not no_ar(u)]
        if faltam:
            time.sleep(15)
    if faltam:
        sys.exit("ERRO: ainda fora do ar depois de 10 min:\n" + "\n".join(faltam))
    print("todos os arquivos estão no ar.")


def cmd_limpa(a):
    d = carrega()
    n = 0
    for it in d["itens"]:
        if it["status"] in ("publicado", "cancelado") and it.get("video"):
            pasta = os.path.join(AQUI, os.path.dirname(it["video"]))
            if os.path.isdir(pasta):
                shutil.rmtree(pasta)
            it["video"] = it["capa"] = it["story"] = None
            it["arquivos_apagados"] = agora().isoformat()
            n += 1
    salva(d)
    print(f"{n} post(s) limpos. Rode 'python fila.py push' para mandar.")


def cmd_marca(a):
    d = carrega()
    it = item(d, a.id)
    it.update(status="publicado", media_id=a.media_id, permalink=a.permalink,
              publicado_em=agora().isoformat(), obs="publicado à mão")
    salva(d)
    print("marcado como publicado:", a.id)


def cmd_devidos(a):
    d = carrega()
    t = agora()
    saida = []
    for it in sorted(d["itens"], key=lambda x: (x["quando"], x["tipo"] != "reel")):
        if it["status"] != "aprovado":
            continue
        dif_min = (quando_de(it["quando"]) - t).total_seconds() / 60
        if -a.antes <= dif_min <= a.depois:
            saida.append({
                "id": it["id"], "quando": it["quando"], "tipo": it["tipo"],
                "video_url": BASE_URL + it["video"], "cover_url": BASE_URL + it["capa"],
                "story_url": BASE_URL + it["story"] if it.get("story") else None,
                "caption": it["legenda"], "comentario": it.get("comentario") or "",
                "trial_params": it.get("trial"),
            })
    print(json.dumps({"agora": t.isoformat(), "itens": saida}, ensure_ascii=False, indent=2))


def cmd_trava(a):
    d = carrega()
    it = item(d, a.id)
    if it["status"] != "aprovado":
        sys.exit(f"ERRO: {a.id} está como {it['status']} (só publica 'aprovado')")
    it["status"] = "publicando"
    it["tentativas"] = it.get("tentativas", 0) + 1
    salva(d)
    print("travado:", a.id)


def cmd_resultado(a):
    d = carrega()
    it = item(d, a.id)
    if a.ok:
        it.update(status="publicado", media_id=a.media_id, permalink=a.permalink,
                  publicado_em=agora().isoformat(), erro=None)
    else:
        it.update(status="erro", erro=a.erro)
    if a.obs:
        it["obs"] = a.obs
    salva(d)
    print(f"{a.id}: {it['status']}")


def main():
    p = argparse.ArgumentParser(description="Fila de publicação do @dobrando_chapa")
    s = p.add_subparsers(dest="cmd", required=True)
    x = s.add_parser("add"); x.add_argument("pasta"); x.add_argument("--quando", required=True)
    x.add_argument("--id"); x.add_argument("--trial", choices=["SS_PERFORMANCE", "MANUAL"])
    x.add_argument("--aprovado", action="store_true"); x.add_argument("--forca", action="store_true")
    x.set_defaults(f=cmd_add)
    x = s.add_parser("aprova"); x.add_argument("ids", nargs="+"); x.set_defaults(f=cmd_aprova)
    x = s.add_parser("cancela"); x.add_argument("id"); x.set_defaults(f=cmd_cancela)
    x = s.add_parser("lista"); x.set_defaults(f=cmd_lista)
    x = s.add_parser("push"); x.add_argument("--msg"); x.set_defaults(f=cmd_push)
    x = s.add_parser("limpa"); x.set_defaults(f=cmd_limpa)
    x = s.add_parser("marca"); x.add_argument("id"); x.add_argument("--media-id", required=True)
    x.add_argument("--permalink"); x.set_defaults(f=cmd_marca)
    x = s.add_parser("devidos"); x.add_argument("--antes", type=float, default=180)
    x.add_argument("--depois", type=float, default=20); x.set_defaults(f=cmd_devidos)
    x = s.add_parser("trava"); x.add_argument("id"); x.set_defaults(f=cmd_trava)
    x = s.add_parser("resultado"); x.add_argument("id")
    g = x.add_mutually_exclusive_group(required=True)
    g.add_argument("--ok", action="store_true"); g.add_argument("--erro")
    x.add_argument("--media-id"); x.add_argument("--permalink"); x.add_argument("--obs")
    x.set_defaults(f=cmd_resultado)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
