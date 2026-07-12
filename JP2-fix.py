# -*- coding: utf-8 -*-
import argparse
import io
import os
import re
import sys
import time
import zipfile

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "https://www.digital.archives.go.jp"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DEFAULT_LIMIT = 100
TIMEOUT = 60
RETRIES = 5
RETRY_WAIT = 2.0

INVALID_FN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")


def log(msg):
    print(msg, flush=True)


def err(msg):
    print(msg, file=sys.stderr, flush=True)


class Net:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "ja,en;q=0.9",
        })

    def get(self, url, **kw):
        kw.setdefault('timeout', TIMEOUT)
        last = None
        for i in range(RETRIES):
            try:
                r = self.s.get(url, **kw)
                return r
            except Exception as e:
                last = e
                if i < RETRIES - 1:
                    time.sleep(RETRY_WAIT * (i + 1))
        err(f"  [网络错误] {type(last).__name__}: {last}  ({url})")
        return None

    def post(self, url, **kw):
        kw.setdefault('timeout', TIMEOUT)
        last = None
        for i in range(RETRIES):
            try:
                return self.s.post(url, **kw)
            except Exception as e:
                last = e
                if i < RETRIES - 1:
                    time.sleep(RETRY_WAIT * (i + 1))
        err(f"  [网络错误] {type(last).__name__}: {last}  ({url})")
        return None


def parse_id(url):
    url = url.strip()
    m = re.search(r'/(?:img|item|file)/(\d+)', url)
    if m:
        return m.group(1)
    m = re.match(r'^\s*(\d+)\s*$', url)
    if m:
        return m.group(1)
    return None


def clean_name(s):
    if not s:
        return "untitled"
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = INVALID_FN.sub("_", s)
    return s or "untitled"


class Meta:
    def __init__(self, manifest):
        self.mf = manifest
        self.label = manifest.get("label", "")
        self.meta = {}
        for item in manifest.get("metadata", []):
            k = item.get("label", "")
            v = item.get("value", [])
            if isinstance(v, list):
                v = " ".join(str(x) for x in v)
            self.meta[k] = str(v)
        src = self.meta.get("Source(URI)", "")
        self.is_book = "/file/" in src
        self.source_uri = src
        self.title = self.meta.get("タイトル(Title)", self.label)
        self.identifier = self.meta.get("請求番号(Identifier:Reference Code)", "")

    def volume_number(self):
        ident = self.identifier.translate(ZEN2HAN)
        m = re.search(r'-(\d+)\s*$', ident)
        if m:
            return int(m.group(1))
        return None

    def base_title(self):
        parts = re.split(r'[\s\u3000]+', self.title.strip())
        base = parts[0] if parts else self.title
        base = re.sub(r'\d+$', '', base)
        return clean_name(base) if base else clean_name(self.title)

    def volume_marker(self):
        parts = re.split(r'[\s\u3000]+', self.title.strip())
        if len(parts) > 1:
            return clean_name(" ".join(parts[1:]))
        m = re.search(r'(\d+)$', parts[0] if parts else self.title)
        if m:
            return m.group(1)
        return None

    def title_volume_number(self):
        marker = self.volume_marker()
        if not marker:
            return None
        marker_han = marker.translate(ZEN2HAN)
        m = re.search(r'(\d+)', marker_han)
        return int(m.group(1)) if m else None

    def filename(self, index=None, total=None):
        base = self.base_title()
        vol = self.volume_number()
        marker = self.volume_marker()
        title_vol = self.title_volume_number()
        multi = total is not None and total > 1

        if multi:
            n = index if index is not None else (vol or 1)
            return f"{base}-第{n}卷"
        if title_vol is not None:
            return f"{base}-第{title_vol}卷"
        if marker and vol is not None:
            return f"{base}-第{vol}卷"
        if marker:
            return f"{base}-{marker}"
        return base or clean_name(self.title)


def get_manifest(net, item_id):
    url = f"{BASE}/api/iiif/{item_id}/manifest.json"
    r = net.get(url)
    if r is None or r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def get_canvas_cids(manifest):
    cids = []
    seqs = manifest.get("sequences", [])
    if not seqs:
        return cids
    for cv in seqs[0].get("canvases", []):
        canvas_id = cv.get("@id", "").rsplit("/", 1)[-1]
        if not canvas_id:
            continue
        prefix = ""
        imgs = cv.get("images", [])
        if imgs:
            svc = imgs[0].get("resource", {}).get("service", {}).get("@id", "")
            m = re.search(r'/item/([^/]+)/', svc)
            if m:
                prefix = m.group(1)
        cid = f"{prefix}/{canvas_id}" if prefix else canvas_id
        cids.append(cid)
    return cids


def get_image_urls(manifest):
    urls = []
    for cv in manifest.get("sequences", [{}])[0].get("canvases", []):
        for img in cv.get("images", []):
            u = img.get("resource", {}).get("@id")
            if u:
                urls.append(u)
    return urls


def _nav_id(html, btn_class):
    for m in re.finditer(r'<button([^>]*)>', html):
        attrs = m.group(1)
        if btn_class in attrs:
            dh = re.search(r'data-href="/img/(\d+)"', attrs)
            if dh:
                return dh.group(1)
    return None


def _fetch_nav_fast(net, item_id):
    try:
        r = net.s.get(f"{BASE}/img/{item_id}", stream=True, timeout=TIMEOUT)
    except Exception:
        return None, None, 0, None
    if r is None or r.status_code != 200:
        if r is not None:
            r.close()
        return None, None, 0, None
    buf = ""
    next_id = None
    first_id = None
    total = 0
    subject = None
    try:
        for chunk in r.iter_content(8192, decode_unicode=True):
            buf += chunk
            if next_id is None:
                m = (re.search(r'nav-btn--next[^>]*data-href="/img/(\d+)"', buf)
                     or re.search(r'data-href="/img/(\d+)"[^>]*nav-btn--next', buf))
                if m:
                    next_id = m.group(1)
            if first_id is None:
                m = (re.search(r'nav-btn--first[^>]*data-href="/img/(\d+)"', buf)
                     or re.search(r'data-href="/img/(\d+)"[^>]*nav-btn--first', buf))
                if m:
                    first_id = m.group(1)
            if total == 0:
                mt = re.search(r'全(\d+)点', buf)
                if mt:
                    total = int(mt.group(1))
            if subject is None:
                ms = re.search(r'viewer-header__subject-title[^>]*>([^<]+)<', buf)
                if ms:
                    subject = ms.group(1).strip()
            if total and subject and (next_id or first_id):
                break
            if len(buf) > 524288:
                break
    except Exception:
        r.close()
        return None, None, 0, None
    r.close()
    return next_id, first_id, total, subject


def find_book_items(net, book_id):
    first_id, _, _, _ = _fetch_nav_fast(net, book_id)
    if not first_id:
        r = net.get(f"{BASE}/img/{book_id}")
        if r is None:
            return [], 0
        html = r.text
        first_id = _nav_id(html, "nav-btn--next")
        if not first_id:
            for m in re.finditer(r'/img/(\d+)', html):
                if m.group(1) != book_id:
                    first_id = m.group(1)
                    break
    if not first_id:
        return [], 0

    first_next, _, total, _ = _fetch_nav_fast(net, first_id)
    ids = _enumerate_next_fast(net, first_id, first_next, total, 0)
    return ids, (total or len(ids))


def _enumerate_next_fast(net, start_id, start_next, total, max_count=0):
    ids = [start_id]
    seen = {start_id}
    cur_next = start_next
    guard_max = max((max_count or total or 0) + 5, 5000)
    guarded = 0
    while guarded < guard_max:
        if max_count and len(ids) >= max_count:
            break
        if total and len(ids) >= total:
            break
        guarded += 1
        nxt = cur_next
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        ids.append(nxt)
        if len(ids) % 20 == 0:
            log(f"  枚举中... 已找到 {len(ids)} 卷")
        cur_next, _, _, _ = _fetch_nav_fast(net, nxt)
    return ids


def find_item_siblings(net, item_id, max_count):
    scope = f"前 {max_count} 卷" if max_count else "全部"
    first_next, first_id, total, _ = _fetch_nav_fast(net, item_id)
    if first_id and first_id != item_id:
        log(f"  从首件 {first_id} 开始枚举...")
        start_id = first_id
        start_next, _, _, _ = _fetch_nav_fast(net, start_id)
    else:
        start_id = item_id
        start_next = first_next
    log(f"  枚举中（{scope}）...")
    ids = _enumerate_next_fast(net, start_id, start_next, total, max_count)
    log(f"  找到 {len(ids)} 卷" + (f"（全 {total} 点）" if total else ""))
    return ids, total


def warm_session(net, item_id):
    net.get(f"{BASE}/img/{item_id}")


def download_pdf_chunk(net, item_id, cids):
    url = f"{BASE}/contentDownload/{item_id}?type=imagePrint"
    data = [("cid", c) for c in cids]
    r = net.post(url, data=data, headers={
        "Referer": f"{BASE}/img/{item_id}",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/pdf,*/*",
    }, stream=True, timeout=180)
    if r is None:
        return None
    if r.status_code != 200:
        r.close()
        err(f"  [官方下载] HTTP {r.status_code}")
        return None
    buf = bytearray()
    try:
        for chunk in r.iter_content(8192):
            if chunk:
                buf.extend(chunk)
    finally:
        r.close()
    data_bytes = bytes(buf)
    if data_bytes[:4] == b"%PDF":
        return data_bytes
    err(f"  [官方下载] 返回非 PDF（{len(data_bytes)} 字节，magic={data_bytes[:8]!r}）")
    return None


def download_image_zip(net, item_id, cids, fmt):
    dl_type = 'imageJp2' if fmt == 'jp2' else 'imageJpeg'
    url = f"{BASE}/contentDownload/{item_id}?type={dl_type}"
    data = [("cid", c) for c in cids]
    r = net.post(url, data=data, headers={
        "Referer": f"{BASE}/img/{item_id}",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/zip,*/*",
    }, stream=True, timeout=180)
    if r is None:
        return None
    if r.status_code != 200:
        r.close()
        err(f"  [官方图片下载] HTTP {r.status_code}")
        return None
    buf = bytearray()
    try:
        for chunk in r.iter_content(8192):
            if chunk:
                buf.extend(chunk)
    finally:
        r.close()
    data_bytes = bytes(buf)
    if data_bytes[:2] == b"PK":
        return data_bytes
    err(f"  [官方图片下载] 返回非 ZIP（{len(data_bytes)} 字节，magic={data_bytes[:8]!r}）")
    return None


def extract_zip_pages(zip_bytes, vol_dir, ext, start_page=1):
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = sorted(zf.namelist())
    count = 0
    for i, name in enumerate(names, start_page):
        with zf.open(name) as ff:
            content = ff.read()
        out_path = os.path.join(vol_dir, f"{i:04d}{ext}")
        with open(out_path, "wb") as f:
            f.write(content)
        count += 1
    return count


def merge_pdfs(pdf_paths, out_path):
    try:
        import pikepdf
    except Exception:
        err("  [合并] 需要 pikepdf，跳过合并（pip install pikepdf）")
        return False
    out = pikepdf.Pdf.new()
    added = 0
    try:
        for p in pdf_paths:
            try:
                src = pikepdf.open(p)
                out.pages.extend(src.pages)
                added += len(src.pages)
                src.close()
            except Exception as e:
                err(f"  [合并] 跳过损坏文件 {p}: {e}")
        if added == 0:
            err("  [合并] 无有效页面可合并")
            return False
        out.save(out_path)
    finally:
        out.close()
    return True


def download_item_pdf(net, item_id, out_dir, limit=DEFAULT_LIMIT, force=False, idx=None, total=None):
    mf = get_manifest(net, item_id)
    if not mf:
        err(f"  [件名 {item_id}] 无法获取 manifest")
        return None
    meta = Meta(mf)
    cids = get_canvas_cids(mf)
    npages = len(cids)
    log(f"  → {meta.title}  （{npages} 页, 请求番号 {meta.identifier or '无'}）")

    if npages == 0:
        err(f"  [件名 {item_id}] 无页面")
        return None

    name = meta.filename(index=idx, total=total)
    out_path = os.path.join(out_dir, f"{clean_name(name)}.pdf")

    if os.path.exists(out_path) and not force:
        log(f"  ✓ 已存在，跳过：{os.path.basename(out_path)}")
        return out_path

    warm_session(net, item_id)

    if npages <= limit:
        log(f"  下载中（{npages} 页）...")
        pdf = download_pdf_chunk(net, item_id, cids)
        if pdf is None:
            pdf = download_iiif_fallback(net, mf, item_id)
        if pdf is None:
            err(f"  [件名 {item_id}] 下载失败")
            return None
        with open(out_path, "wb") as f:
            f.write(pdf)
        log(f"  ✓ 已保存：{os.path.basename(out_path)}  ({len(pdf)/1024/1024:.1f} MB)")
        return out_path

    log(f"  页数 {npages} > {limit}，分块下载...")
    tmp_dir = os.path.join(out_dir, f"_tmp_{item_id}")
    os.makedirs(tmp_dir, exist_ok=True)
    parts = []
    ok = True
    for start in range(0, npages, limit):
        chunk = cids[start:start + limit]
        n = start // limit + 1
        ntotal = (npages + limit - 1) // limit
        log(f"  块 {n}/{ntotal}（第 {start+1}-{start+len(chunk)} 页）...")
        pdf = download_pdf_chunk(net, item_id, chunk)
        if pdf is None:
            sub_mf = {"sequences": [{"canvases": mf["sequences"][0]["canvases"][start:start+len(chunk)]}]}
            pdf = download_iiif_fallback(net, sub_mf, item_id)
        if pdf is None:
            err(f"  块 {n} 失败")
            ok = False
            break
        p = os.path.join(tmp_dir, f"part_{n:03d}.pdf")
        with open(p, "wb") as f:
            f.write(pdf)
        parts.append(p)
    if ok and parts:
        if len(parts) == 1:
            os.replace(parts[0], out_path)
        else:
            if not merge_pdfs(parts, out_path):
                err(f"  [件名 {item_id}] 合并失败，分块保留于 {tmp_dir}")
                return None
        for p in parts:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
        log(f"  ✓ 已保存：{os.path.basename(out_path)} （{len(parts)} 块合并）")
        return out_path
    for p in parts:
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass
    return None


def download_iiif_fallback(net, manifest, item_id):
    try:
        from PIL import Image
    except Exception:
        err("  [回退] 需要 pillow")
        return None
    img_urls = get_image_urls(manifest)
    if not img_urls:
        return None
    log(f"  [回退 IIIF] 下载 {len(img_urls)} 页...")
    images = []
    for i, u in enumerate(img_urls, 1):
        r = net.get(u)
        if r is None or r.status_code != 200:
            err(f"  [回退] 第 {i} 页图像下载失败")
            return None
        try:
            im = Image.open(io.BytesIO(r.content)).convert("RGB")
            images.append(im)
        except Exception as e:
            err(f"  [回退] 第 {i} 页解析失败: {e}")
            return None
        if i % 10 == 0:
            log(f"    已取 {i}/{len(img_urls)} 页")
    if not images:
        return None
    buf = io.BytesIO()
    first = images[0]
    rest = images[1:]
    first.save(buf, format="PDF", save_all=True, append_images=rest, resolution=300.0)
    return buf.getvalue()


def download_item_images(net, item_id, out_dir, fmt='jpeg', force=False, idx=None, total=None):
    mf = get_manifest(net, item_id)
    if not mf:
        err(f"  [件名 {item_id}] 无法获取 manifest")
        return None
    meta = Meta(mf)
    cids = get_canvas_cids(mf)
    npages = len(cids)
    log(f"  → {meta.title}  （{npages} 页, 请求番号 {meta.identifier or '无'}）")

    if npages == 0:
        err(f"  [件名 {item_id}] 无页面")
        return None

    name = meta.filename(index=idx, total=total)
    vol_dir = os.path.join(out_dir, clean_name(name))
    ext = '.jp2' if fmt == 'jp2' else '.jpg'

    if os.path.isdir(vol_dir):
        existing = [f for f in os.listdir(vol_dir) if f.endswith(ext)]
        if len(existing) >= npages and not force:
            log(f"  ✓ 已存在，跳过：{os.path.basename(vol_dir)}/")
            return vol_dir

    os.makedirs(vol_dir, exist_ok=True)
    warm_session(net, item_id)

    fmt_label = 'JPEG2000' if fmt == 'jp2' else 'JPEG'
    log(f"  下载 {npages} 页图片（{fmt_label}）...")

    img_chunk = 20 if fmt == 'jp2' else 50
    if npages <= img_chunk:
        zip_bytes = download_image_zip(net, item_id, cids, fmt)
        if zip_bytes is not None:
            n = extract_zip_pages(zip_bytes, vol_dir, ext)
            log(f"  ✓ 已保存：{os.path.basename(vol_dir)}/  ({n} 页)")
            return vol_dir
        log(f"  官方接口失败，回退 IIIF...")

    ok = _download_images_chunked(net, item_id, mf, cids, vol_dir, ext, fmt, img_chunk)
    if ok:
        log(f"  ✓ 已保存：{os.path.basename(vol_dir)}/  ({npages} 页)")
        return vol_dir
    err(f"  [件名 {item_id}] 下载失败")
    return None


def _download_images_chunked(net, item_id, mf, cids, vol_dir, ext, fmt, chunk_size=20):
    npages = len(cids)
    img_urls = get_image_urls(mf)
    page = 0
    for start in range(0, npages, chunk_size):
        chunk = cids[start:start + chunk_size]
        n = start // chunk_size + 1
        ntotal = (npages + chunk_size - 1) // chunk_size
        log(f"  块 {n}/{ntotal}（第 {start+1}-{start+len(chunk)} 页）...")
        zip_bytes = download_image_zip(net, item_id, chunk, fmt)
        if zip_bytes is not None:
            page += extract_zip_pages(zip_bytes, vol_dir, ext, start_page=start + 1)
            continue
        log(f"  块 {n} 失败，回退 IIIF...")
        for i in range(len(chunk)):
            if start + i >= len(img_urls):
                break
            r = net.get(img_urls[start + i])
            if r is None or r.status_code != 200:
                err(f"  第 {start + i + 1} 页 IIIF 下载失败")
                return False
            if fmt == 'jp2':
                try:
                    from PIL import Image
                    im = Image.open(io.BytesIO(r.content))
                    im.save(os.path.join(vol_dir, f"{start + i + 1:04d}.jp2"),
                            format='JPEG2000')
                except Exception as e:
                    err(f"  第 {start + i + 1} 页转换 JP2 失败: {e}")
                    return False
            else:
                with open(os.path.join(vol_dir, f"{start + i + 1:04d}.jpg"), "wb") as f:
                    f.write(r.content)
            page += 1
        if page % 10 == 0:
            log(f"    已下载 {page}/{npages} 页")
    return page >= npages


def ask_download_scope(total):
    log(f"\n共 {total} 卷：")
    log("  1) 全部")
    log("  2) 前 N 卷")
    log("  3) 跳过")
    while True:
        try:
            choice = input("请选择 [1/2/3]（默认1）: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice in ('1', ''):
            return 0
        if choice == '3':
            return -1
        if choice == '2':
            while True:
                try:
                    s = input(f"卷数 (1-{total}): ").strip()
                except (EOFError, KeyboardInterrupt):
                    return 0
                if s.isdigit() and 1 <= int(s) <= total:
                    return int(s)
                err(f"  请输入 1-{total}")
        else:
            err("  请输入 1、2 或 3")


def ask_download_format():
    log("\n请选择下载格式：")
    log("  1) PDF")
    log("  2) JPEG")
    log("  3) JPEG2000")
    while True:
        try:
            choice = input("请选择 [1/2/3]（默认1）: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 'pdf'
        if choice in ('1', ''):
            return 'pdf'
        if choice == '2':
            return 'jpeg'
        if choice == '3':
            return 'jp2'
        err("  请输入 1、2 或 3")


def _download_volumes(net, ids, total, meta, out_dir, limit, force,
                      folder_name=None, fmt='pdf'):
    if not ids:
        return
    book_dir = os.path.join(out_dir, folder_name or clean_name(meta.base_title()))
    os.makedirs(book_dir, exist_ok=True)
    log(f"  输出目录：{os.path.basename(book_dir)}/")
    for i, cid in enumerate(ids, 1):
        log(f"\n[{i}/{len(ids)}] 卷 ID={cid}")
        if fmt == 'pdf':
            download_item_pdf(net, cid, book_dir, limit, force, idx=i, total=total or len(ids))
        else:
            download_item_images(net, cid, book_dir, fmt, force, idx=i, total=total or len(ids))


def process_one(net, url, out_dir, limit, force, max_volumes=0, fmt='pdf'):
    item_id = parse_id(url)
    if not item_id:
        err(f"无法解析链接：{url}")
        return

    mf = get_manifest(net, item_id)
    if not mf:
        err(f"无法获取 manifest：{url}")
        return
    meta = Meta(mf)
    log("")

    if meta.is_book:
        log(f"■ 簿冊：{meta.title}  ID={item_id}")
        ids, total = find_book_items(net, item_id)
        if not ids:
            log(f"  未找到子件名，按单件下载...")
            if fmt == 'pdf':
                download_item_pdf(net, item_id, out_dir, limit, force)
            else:
                download_item_images(net, item_id, out_dir, fmt, force)
            return
        total_volumes = total or len(ids)
        if max_volumes:
            n = max_volumes
        elif total_volumes > 1:
            n = ask_download_scope(total_volumes)
            if n == -1:
                return
        else:
            n = 0
        if n and len(ids) > n:
            log(f"  共 {total_volumes} 卷，下载前 {n} 卷")
            ids = ids[:n]
        else:
            log(f"  共 {total_volumes} 卷，全部下载")
        _download_volumes(net, ids, len(ids), meta, out_dir, limit, force, fmt=fmt)
        return

    next_id, first_id, total, subject = _fetch_nav_fast(net, item_id)
    if total and total > 1:
        series_title = clean_name(subject) if subject else meta.base_title()
        log(f"■ 件名系列：{subject or meta.title}  ID={item_id}（全 {total} 点）")
        if max_volumes:
            n = max_volumes
        else:
            n = ask_download_scope(total)
            if n == -1:
                return
        if first_id and first_id != item_id:
            log(f"  从首件 {first_id} 开始枚举...")
            start_id = first_id
            start_next, _, _, _ = _fetch_nav_fast(net, start_id)
        else:
            start_id = item_id
            start_next = next_id
        ids = _enumerate_next_fast(net, start_id, start_next, total, n)
        log(f"  将下载 {len(ids)} 卷")
        _download_volumes(net, ids, len(ids), meta, out_dir, limit, force,
                          folder_name=series_title, fmt=fmt)
        return

    log(f"■ 件名：{meta.title}  ID={item_id}")
    if fmt == 'pdf':
        download_item_pdf(net, item_id, out_dir, limit, force)
    else:
        download_item_images(net, item_id, out_dir, fmt, force)


def main():
    ap = argparse.ArgumentParser(
        description="国立公文書館批量下载工具")
    ap.add_argument("urls", nargs="*", help="一个或多个 img/item/file 链接或纯 ID")
    ap.add_argument("-o", "--output", default=".", help="输出目录（默认当前目录）")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help=f"单次下载页数上限（默认{DEFAULT_LIMIT}）")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    ap.add_argument("--max-volumes", type=int, default=0,
                   help="最多下载卷数（默认0=全部）")
    ap.add_argument("--format", choices=['pdf', 'jpeg', 'jp2'], default=None,
                   help="下载格式：pdf/jpeg/jp2，不指定则交互询问")
    args = ap.parse_args()

    out_dir = args.output
    os.makedirs(out_dir, exist_ok=True)
    net = Net()

    urls = list(args.urls)
    if not urls:
        log("请输入链接（每行一个，空行结束）：")
        while True:
            try:
                line = input().strip()
            except EOFError:
                break
            if not line:
                break
            urls.append(line)

    if not urls:
        log("未提供任何链接。")
        return

    fmt = args.format or ask_download_format()

    log(f"共 {len(urls)} 个链接，输出目录：{os.path.abspath(out_dir)}")
    for u in urls:
        try:
            process_one(net, u, out_dir, args.limit, args.force, args.max_volumes, fmt)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            err(f"处理出错 {u}: {type(e).__name__}: {e}")
    log("\n全部完成。")


if __name__ == "__main__":
    main()
