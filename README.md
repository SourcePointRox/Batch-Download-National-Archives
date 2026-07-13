# Batch-Download-National-Archives
Note!!!:The original script(na_download.py) downloads JPEG2000 files converted from standard JPEGs rather than the official source to accommodate the workflow.
If you need to pull JPEG2000 files directly from the official source, download the JP2-fix.py script.

Code for batch downloading resources from the National Archives of Japan following the website URL format revision in 2026

Code Dependencies:requests, pikepdf, pillow（IIIF)

Installation：
```bash
pip install requests pikepdf pillow
```

The code flowchart (AI-generated) is as follows:
```text
┌─────────────────────────────────────────────────────┐
│                    程序启动                           │
│  py na_download.py [URLs] [-o 目录] [--format FMT]  │
│                       [--max-volumes N] [--force]    │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │  命令行提供了 URL？   │
              └──────┬───────┬───────┘
                     │否     │是
                     ▼       │
        ┌────────────────────┐ │
        │ 交互输入链接        │ │
        │ (逐行输入，空行结束) │ │
        └────────┬───────────┘ │
                 │             │
                 └──────┬──────┘
                        │
                        ▼
              ┌──────────────────────┐
              │ 指定了 --format？     │
              └──────┬───────┬───────┘
                     │否     │是
                     ▼       │
        ┌──────────────────────┐  │
        │  询问下载格式         │  │
        │  1) PDF (默认)        │  │
        │  2) JPEG              │  │
        │  3) JPEG2000          │  │
        └────────┬─────────────┘  │
                 │                │
                 └───────┬────────┘
                         │
                    fmt = pdf / jpeg / jp2
                         │
                         ▼
        ┌────────────────────────────────┐
        │      遍历每个 URL               │
        │  解析 ID → 获取 manifest        │
        └───────────────┬────────────────┘
                        │
                        ▼
              ┌──────────────────────┐
              │  簿冊 or 件名？       │
              └──┬───────────────┬───┘
            簿冊 │               │ 件名
                 ▼               ▼
     ┌──────────────────┐  ┌──────────────────────┐
     │ 枚举子件名        │  │  快速检查系列         │
     │ find_book_items  │  │  _fetch_nav_fast     │
     └────────┬─────────┘  │  → next/first/total  │
              │            └──────────┬───────────┘
              ▼                       │
     ┌────────────────┐        ┌──────┴──────┐
     │ 多卷且无       │ 是     │ total > 1？ │
     │ --max-volumes？├───→ 询问│      │      │
     │      │ 否      │ 下载范围├──否──┤      │
     │      ▼         │ 1全部  │      │      │
     │  直接下载全部  │ 2前N卷 │      ▼      ▼
     │  或前N卷       │ 3跳过  │  多卷系列   单卷/单图
     └───────┬────────┘       │      │        │
             │                ▼      │        │
             │     ┌──────────────────┐│        │
             │     │ 非首件？跳到首件  ││        │
             │     │ 从首件开始枚举    ││        │
             │     └────────┬─────────┘│        │
             │              │          │        │
             └──────────────┴──────────┘        │
                        │                       │
                        ▼                       │
         ┌──────────────────────────┐           │
         │   根据 fmt 选择下载方式    │◄──────────┘
         └──────────┬───────────────┘
                    │
          ┌─────────┼─────────┐
          │         │         │
       pdf│      jpeg│       jp2
          ▼         ▼         ▼
   ┌──────────┐ ┌──────────┐ ┌──────────────┐
   │ 官方接口  │ │ IIIF下载 │ │ IIIF下载JPEG │
   │ 下载PDF  │ │ 每页JPEG │ │ →Pillow转换  │
   │ (>100页  │ │ 存入卷   │ │ →存为.jp2    │
   │  分块合并)│ │ 子文件夹 │ │ 存入卷子文件夹│
   └──────────┘ └──────────┘ └──────────────┘

  输出目录结构：
  输出目录/
    └─ 簿册名/                      ← 系列文件夹
       ├─ 卷名-第1卷.pdf            ← PDF：直接放文件
       ├─ 卷名-第2卷.pdf
       │
       ├─ 卷名-第1卷/               ← JPEG/JP2：卷子文件夹
       │  ├─ 0001.jpg (或 .jp2)
       │  ├─ 0002.jpg
       │  └─ ...
```
Please forgive me if there are any flaws in the code. 
Contact email: jssdfccd@163.com
