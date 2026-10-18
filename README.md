# no_file_mess

Catalog the files on a disk, find byte-identical copies, plan folder renames, and copy the files another catalog does not already have. Plans do not change the disk until `apply`. `apply` moves duplicates into a quarantine folder and renames directories. It does not delete files. `restore` puts a journaled run back.

This draft does not compare similar-looking photos, score blur, or open a review page. The catalog is a SQLite file. Every command except `plan-dates` uses only the Python standard library. `plan-dates` needs exifread.

Requires Python 3.9 or newer.

## Install

Use a virtual environment so `plan-dates` can install exifread without touching the system Python. On macOS the command is `python3`; there is often no `python`. After activation, `python` is the environment.

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
no-file-mess --help
```

The macOS system Python ships with pip 21.2, which cannot install a `pyproject.toml` project. The upgrade fixes that.

On Windows, from the same directory:

```bat
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e .
no-file-mess --help
```

`.venv` stays in the repository directory and is not committed. Activate it again in each new terminal before running a command.

Commands other than `plan-dates` also run with no install, as `PYTHONPATH=src python3 -m no_file_mess`. Tests: `python -m unittest discover -s tests` inside the activated environment.

## Catalog

Pick a volume name. The name is the identity of that disk in the catalog. The directory you pass is only where it is mounted right now.

```bash
PYTHONPATH=src python -m no_file_mess scan --db catalog.sqlite --volume disk-a photos-root
PYTHONPATH=src python -m no_file_mess volumes --db catalog.sqlite
PYTHONPATH=src python -m no_file_mess search --db catalog.sqlite france
PYTHONPATH=src python -m no_file_mess search --db catalog.sqlite --substring 03-Fr
```

A scan replaces the previous file list for that volume. Other volumes are left as they were, including when their disks are unplugged. Search matches path tokens by prefix (`fran` matches `France`). `--substring` matches any path fragment.

Each file is stored with its size, modification time, and SHA-256. The algorithm name is stored next to the digest. Placeholders that are not downloaded, symbolic links, and names such as `.DS_Store` and `Thumbs.db` are skipped.

## Identical files

```bash
PYTHONPATH=src python -m no_file_mess plan-dups --db catalog.sqlite --volume disk-a --out dups.json
```

The plan suggests one keeper per group of identical digests. In order, it prefers:

1. A copy inside a directory whose hashes are a subset of some other directory, and that other directory has more files. Nested directories are not compared with their parents.
2. A copy under a curated prefix from a labels file.
3. A copy that is not in a dump directory. `DCIM`, `WhatsApp`, `WhatsApp Images`, and `Screenshots` are dump names. A labels file can add prefixes.
4. The newer modification time.
5. The shortest path.

`examples/labels.json` shows the labels shape. Edit the plan before applying it. Delete any quarantine line you do not want.

```bash
PYTHONPATH=src python -m no_file_mess apply dups.json --root photos-root
PYTHONPATH=src python -m no_file_mess restore --root photos-root
```

Quarantined files go to `photos-root/.no-file-mess-quarantine/files/`, and the journal is `journal.jsonl` beside that. A file whose bytes no longer match the plan is left in place and the command stops.

## Folder names

The recognizer looks for one year (1900–2099). A month, when present, may be an English name (`Mar` or `March`) or a number from 1 to 12. A day is recognized when a month was found. A year followed by two numbers is month then day, including when the day is 12 or below (`2026-08-02 France` becomes `2026-08-02-France`). Two numbers before the year, such as `08-02-2026`, are left unchanged. A title with two years is left unchanged. The other words stay in their original order. Two month numbers, two years, or two day numbers are reported and not renamed. A title with no year is ignored.

The default template is `{year}-{month}-{day}-{name}`. A missing day or month is left out, along with the hyphen in front of it. `March 2026 South of France` becomes `2026-03-South-of-France`, `15 March 2026 France` becomes `2026-03-15-France`, and `Queen of Hearts 2018` becomes `2018-Queen-of-Hearts`. `Mar` and `March` are both March. `{year}`, `{month}`, `{day}`, and `{name}` are the placeholders. Put them in the `template` field of the rules file.

```bash
PYTHONPATH=src python -m no_file_mess plan-names --root photos-root --out names.json
PYTHONPATH=src python -m no_file_mess plan-names --root photos-root --rules examples/folders.json --out names.json
PYTHONPATH=src python -m no_file_mess apply names.json --root photos-root
```

Rules fields:

| Field | Default | Meaning |
| --- | --- | --- |
| `template` | `{year}-{month}-{day}-{name}` | Written name. An empty placeholder is skipped, with the separator before it |
| `separator` | `-` | Joiner between the leftover words |
| `year_only` | true | When false, a title with a year and no month is left unchanged |

Two folders that would become the same name, or a rename onto a name that already exists, are notes. Notes are not applied. The root directory itself is not renamed.

## Capture dates

`plan-dates` reads EXIF `DateTimeOriginal` from JPEG and HEIC through [exifread](https://github.com/ianare/exif-py) (BSD-3-Clause). When that tag is missing, a date in the filename is used instead. Phone names that carry one include `PXL_20260315_123045123.jpg`, `Screenshot20260315-120405.png`, and `WhatsApp Image 2026-03-15 at 12.04.05.jpeg`. A counter such as `IMG_4521.HEIC` is not a date, so those files count only when EXIF is present.

The proposed month is the earliest month among the files placed directly in the folder. The day is not written into the folder name. A folder already named for that earliest month is left alone when every other file falls within two months after it: photos from March through May may stay in a March folder. `--slack 2` is that allowance. A wider span is a note, not a rename, when the folder is already the earliest month. Any other month is a proposed rename to the earliest one.

```bash
PYTHONPATH=src python -m no_file_mess plan-dates --root photos-root --out dates.json
PYTHONPATH=src python -m no_file_mess plan-dates --root photos-root --rules examples/folders.json --slack 2 --out dates.json
PYTHONPATH=src python -m no_file_mess plan-dates --root photos-root --interactive --out dates.json
PYTHONPATH=src python -m no_file_mess apply dates.json --root photos-root
```

`--interactive` asks for a key. A letter acts immediately. Enter selects the capital letter in the prompt: `r`, or `y` when the photos cover more than one month. `!` uses that default for the current folder and every folder after it. A folder that already has a year and no month is left alone when that year is the year of most of its photos. Press `h` to see the keys again while a folder is waiting.

None of those keys rename or move anything. After the last folder, the planned renames are listed and the prompt is `apply these changes? [y/n]`. Enter does not answer it. `y` performs the renames and moves. `n` leaves the files where they are. The plan file is written either way.

| Key | Action |
| --- | --- |
| `s` | Skip the folder |
| `r` | Rename the whole folder to the earliest month, keeping the rest of the name as written |
| `y` | Year only. The year is the one with the most photos; a tie uses the later year |
| `m` | Move each dated file into its own month folder. Offered when the photos cover more than one month |
| `q` | Stop asking. Choices already made stay in the plan; later folders are left out |
| `!` | Use the default for this folder and every folder after it |
| `h` | Explain the keys |
| Enter | The capital letter: `r`, or `y` when the photos cover more than one month |

Files with no capture date stay in the original folder. `m` removes that folder only when it is empty afterwards. `restore` puts the files back.

`plan-dates` needs exifread, which the install step above provides. The other commands do not.

## Bundle

Scan both disks, at different times if they are never mounted together. Then, with one disk on hand, copy the bytes the other catalog lacks:

```bash
PYTHONPATH=src python -m no_file_mess bundle \
  --db catalog.sqlite \
  --have disk-a \
  --from-volume disk-b \
  --source photos-root \
  --out bundle-dir
```

`bundle-dir/files/` repeats the source paths of the missing files. `MANIFEST.json` lists those files, the hashes, and paths that already exist on the other volume under a different name. `README.txt` is a short count of files, bytes, and folders. Same bytes are not copied again. The bundle directory must not already exist.

## Layout

`src/no_file_mess/` is the package. `catalog` stores and searches. `duplicates` and `names` build plans. `plans` applies and restores them. `bundle` writes the copy folder. `cli` is the command line.

## License

Apache License 2.0. See `LICENSE` and `NOTICE`.
