# FlashRescue

Recovers deleted files from USB flash drives, SD/microSD cards, external drives and internal hard drives/SSDs. It supports NTFS (Windows drives), exFAT, FAT32, FAT16, and the ext4 and btrfs filesystems used by NAS boxes. It can also rebuild RAID arrays. FlashRescue only reads from the drive and never writes to it.

**Version 1.7.0**

## Install and update

Download **FlashRescue-Setup-x.y.z.exe** from the [releases page](https://github.com/drac0nik/FlashRescue/releases/latest) and run it. If Windows shows "Windows protected your PC", click **More info > Run anyway** (the installer isn't code-signed). No administrator rights are needed to install. FlashRescue asks for them when it starts, because it reads drives directly.

**Updates:** FlashRescue checks GitHub once a day. When a new version is out, the **Check for updates** button (bottom right) turns into a green **Update to x.y.z**. Click it to see what's new, then **Download and install**. The installer is checked against its published checksum, FlashRescue closes, updates and starts again. Your settings stay as they are. Nothing about you, your drives or your files is sent, only an ordinary request for the latest-release page. You can turn the daily check off in the window that opens when you check and you're already up to date.

**ffmpeg** (for checking and repairing videos) isn't in the installer. The first time it's needed, FlashRescue offers to download it from gyan.dev (about 100 MB, checked against its published checksum) and keeps it for next time.

## What's new

### In-app updates and an installer (1.7.0)
- FlashRescue now has an installer, Start menu and desktop shortcuts, and its own icon.
- It checks for new versions once a day. **Update to x.y.z** downloads the new version, checks it and installs it in one click.
- ffmpeg downloads by itself the first time a video is checked or repaired.

### Faster, gentler imaging and scanning (1.6.0)
- Rescue images copy the file tables and deleted files first, show a progress bar with the time left, and keep the drive streaming while the image is written.
- Deep scans read ahead and make far fewer head movements. The scanner runs in its own process, so the window stays smooth. Check files uses several CPU cores.

### NAS, RAID, ext4 and btrfs (1.5.0)
- Virtual RAID rebuilds RAID 0/1/5/6/10 and JBOD arrays from their drives or images, including NAS (Linux md + LVM) setups.
- ext2/3/4 and btrfs are read directly, with real file names.

## Building it yourself

1. Install Python 3.9+ from python.org (tick **Add python.exe to PATH**).
2. Double-click **Setup.bat**. It builds `FlashRescue.exe` in this folder and downloads ffmpeg (for checking and repairing videos) into `tools`.
3. Run `FlashRescue.exe`. Windows will ask for admin rights, which it needs to read the drive directly.

## Failing or old drive? Read this first

If the drive clicks, disconnects, freezes Explorer, or is very slow, **don't scan it directly**. Use the tools below so it gets read as little as possible.

### Drive health

**Drive health…** reads the drive's own SMART report: reallocated and pending (unreadable) sectors, cable errors, temperature, power-on hours, and SSD wear. It then gives a plain verdict: **Healthy**, **Warning signs** or **FAILING**. A health check also runs automatically before every scan. If the drive reports problems, FlashRescue offers to make a rescue image first.

Most USB sticks, memory cards and many USB adapters don't pass SMART through. "Not available" is normal for those and isn't a sign of a problem.

### Rescue image

**Rescue image (failing drive)…** copies the whole drive to an image file on another drive. All scanning and recovering then happens on the image, so the failing drive is only read once. It works in passes:

0. **File tables and deleted files first** (on by default). A quick scan runs through the imager, so the partition tables, file tables and folders get copied first. Then the data of the deleted files that scan finds is copied, in disk order. In testing, this took about 10% of the drive and already held every file a quick scan of the whole drive finds. If the drive dies an hour in, you still have them.
1. Copies everything that reads easily, skipping bad and slow areas instead of grinding on them.
2. Copies the skipped areas from the other direction.
3. Copies whatever is left.
4. Narrows down each failed area sector by sector, from both ends.
5. Tries the remaining insides of failed areas sector by sector.
6. Retries bad sectors (1 time by default, you can change this).

Details:

- **Map file:** progress is saved to a map file (`yourimage.img.map`) every few seconds. If you press Stop, the PC crashes, or the drive disconnects, choose the same image file again and press **Resume**.
- **Disconnects:** if the drive drops out mid-rescue, the rescue waits and carries on automatically when it's plugged back in.
- **Timeouts:** a read that hangs is abandoned after the timeout (8 seconds by default). Windows would otherwise retry the same bad spot for minutes.
- **Temperature:** the drive rests automatically if it gets too hot (50°C / 122°F by default, checked every minute, needs SMART).
- **Sleep:** the PC won't go to sleep during the rescue.
- **Progress and time left:** a progress bar shows how much is done, plus an estimate of the time left based on the recent read speed. Damaged areas are revisited at the end, and how long that takes depends on the damage, so the estimate says so instead of guessing.
- **Gentle and fast:** writing the image runs on its own thread, so the drive streams continuously instead of pausing for every write. On healthy stretches the reads grow to 4 MB, and they drop back to 1 MB at the first sign of trouble. The map file only ever counts data that has actually been flushed to the image.
- **Block map:** a live grid shows what's rescued (green), not tried yet (grey), being worked on (orange) and bad (red).
- **Scan when done:** when the rescue finishes, the image is scanned for deleted files automatically. Every file that touches an unreadable area is marked, for example "Good, 8.0 KB unreadable".
- **ddrescue compatible:** the map file uses the GNU ddrescue format, so a rescue can be continued with ddrescue on Linux, and a ddrescue image plus map can be opened here with **Open disk image file…**.

Tips for a dying drive:

- If Windows offers to **"Scan and fix"** or **format** the drive, say **No**. Repairs write to the drive and can destroy what's left.
- Use a USB port on the back of the PC, keep the cable still, and give the drive airflow.
- The image needs free space about the size of the drive (less if the drive is partly unreadable).

## Speed and multi-core PCs

The drive is almost always the limit, so most of the work went into reading it as little, and as smoothly, as possible.

- **Deep scan reads ahead:** the next 8 MB chunk is read while the current one is searched, so the drive streams without pausing. Checking a candidate file inside chunks already read never moves the drive head. On a simulated 150 MB/s hard drive, a 2 GB deep scan went from 2,209 head seeks to 133, and from 34 s to 17 s (the transfer alone is 14 s).
- **Fewer seeks for deleted files:** the quick "is this file's data still there?" checks are done in disk order instead of file-table order, which avoids thousands of seeks on a drive with many deleted files.
- **Faster searching:** the deep scan's signature search runs at about 1.6 GB/s per core (was 265 MB/s), so even a fast SSD or an image on NVMe isn't held back by the CPU.
- **Scanner in its own process:** scans of drives and images run in a helper process with its own CPU core, so the window stays smooth however busy the scan is. Virtual RAID volumes are scanned on a thread instead. If the helper can't start, the scan quietly runs on a thread the old way.
- **Smooth list:** results are added in small time slices, and sorting or filtering 100,000 files fills the list in slices instead of freezing the window.
- **Check files uses several cores:** files are copied off the drive one at a time in disk order, then checked several at once (up to 8 at a time).
- **Graphics cards:** an RTX card doesn't help here. Recovery is limited by the drive, and the work the CPU does (parsing file tables, matching signatures) doesn't suit a GPU. Video checking deliberately doesn't use hardware decoding, because GPU decoders hide damage that the software decoder reports.

## What gets found

| Found by | What it is |
|---|---|
| Deleted | Deleted files still listed in the filesystem, with original names, folders and dates. |
| Recycle Bin | Files in (or emptied from) the Windows Recycle Bin, put back under their **original** name and folder (for example `/(Recycle Bin)/C/Users/Anna/Pictures/Beach.jpg`), with the date they were binned. Folders that were binned keep their contents. |
| Before format | Files from **before an NTFS drive was quick-formatted**, even if it was reformatted as exFAT or FAT32. The old file table is rebuilt from leftovers, so names and folders come back, not just numbered files. Found by the deep scan. |
| Lost partition | Files on partitions that were deleted, or whose partition table was wiped (including NTFS partitions with a destroyed boot sector; the backup copy is used). Both normal and deleted files on them are listed. Found by the deep scan. |
| Thumbnail | Small copies of pictures from Windows' thumbnail caches (`thumbcache_*.db`, `Thumbs.db`). Useful when the original photo is gone or overwritten. |
| Deep scan | Files found by their content. Photos and videos are **named by the date they were taken** (from the camera's EXIF data or the video's recording time), for example `2025-06-15 14.30.22.jpg`. |

When a deep-scan file turns up later with its real name (from before a format or a lost partition), the numbered copy is removed automatically.

### Previous Versions (shadow copies)

On Windows, the drive list also shows **snapshots** such as `C: as it was on 2026-11-01 03:00 (Previous Versions snapshot)`. Scanning one with **Also list files that are not deleted** (ticked automatically) shows the files as they were on that date. It's often the only way to get back a file on an SSD that has been TRIMmed, or an older version of a file that was overwritten.

### Check files

**Check files** opens each selected file (or every file shown) and reports whether it is really usable:

- **Pictures** are decoded fully ("Opens fully", "Picture is cut short").
- **Office and zip files** have every part checked.
- **PDFs** are checked for their end markers.
- **Videos and audio** are played through end to end ("Plays fully (3:10)", "Breaks at 0:42 of 3:10", "Won't play - index missing").

### Repair video

**Repair video…** fixes videos that won't play. Most often that's a recording that was cut off (battery died, card pulled) or a file that was only partly recovered. It works without the original phone or camera:

1. If ffmpeg can open the file at all, it's re-wrapped and the damaged tail is dropped.
2. If the index (`moov`) is missing, the video frames are pulled out of the raw data and rebuilt into a new MP4. The codec settings needed to decode them come from, in order:
   - **The video itself.** Many dashcams, action cams, drones and security recorders repeat them inside the video.
   - **A reference video.** Any healthy clip from the **same model** of phone or camera at similar settings. FlashRescue suggests clips from the same scan automatically, since another video on the same card is ideal. You can also browse for one.
   - **Analysis (H.264 only).** The frame headers are analysed and test-decoded to work out the resolution and coding settings. In testing this rebuilt H.264 videos frame-perfectly with no reference at all.

H.265/HEVC videos (recent iPhones and many Android phones) need a reference clip if the settings aren't inside the video. Uncompressed (PCM) audio is rebuilt too. Compressed (AAC) audio usually can't be rebuilt without the index, so repaired phone videos may be silent. Repair and video checks need **ffmpeg**, which Setup.bat downloads into the `tools` folder.

### More tools

- **Filters:** size range, modified-date range, and **Find duplicates** plus **Hide duplicates**. The best copy of each file stays visible; duplicates are spotted by comparing the start and end of same-sized files.
- **Save results… / Load results…:** keep a scan and reopen it later without scanning again. You'll be asked for the drive or its image if it's not connected. The last scan is also saved automatically.

## Before you start

- **Stop using the drive.** Every new file you save to it can overwrite deleted data.
- **Save recovered files to a different drive.** FlashRescue won't let you save to the drive you're scanning. When you scan a whole disk, that covers every partition on it.
- **Recovering from your Windows drive (C:):** Windows writes to it constantly, so act quickly. Close other programs, don't install anything (including recovery tools onto C:), and save recovered files to a USB stick or external drive.

## How to use

1. Pick what to scan and press **Start scan**. The list shows:
   - **Drive letters** (`E:  SANDISK …`): one partition. Removable drives come first. Your Windows drive is marked **WINDOWS DRIVE**.
   - **Whole disks** (`Whole disk 1:  WDC WD10EZEX …`): every partition on that physical disk, plus any unpartitioned space. Use this when a partition was deleted, the disk shows up as unallocated, or you don't know where the files were. Results are grouped as `/Partition 1 (NTFS)/…`. Whole disks only appear when the app runs as administrator.
2. Scan options:
   - **Find deleted files**: fast. Gets back the original names, folders and dates.
   - **Deep scan**: reads the whole drive looking for files by their content. Use it after a format, when Windows says "You need to format the disk", or when the fast scan comes up empty. Results are named `00001.jpg`, etc.
   - **Only search free space**: leave this ticked for normal deletions. **Untick it after the drive has been formatted** so the scan covers everything.
   - **Also list files that are not deleted**: handy when the drive won't open in Windows but its files are still there.
3. Click a file to preview it, or double-click to open a temporary copy in its normal program.
4. Select files (Ctrl+A for all) and press **Recover selected…**, or press **Recover all shown…**. Files go into a new dated `FlashRescue …` folder at the location you pick.
   - Files are read **in disk order**, one smooth sweep from start to end instead of jumping around. This is faster and much gentler on hard drives. Auto-recover does the same.
   - A **`FlashRescue report.csv`** is saved alongside the files. It lists every recovered file, where it came from, its condition, and whether any part of it was unreadable.

### Auto-recover as found

Tick **Save files automatically while scanning**, choose a folder on another drive, and pick which types to keep (for example, only Pictures and Videos). Files are saved in the background as soon as they're found:

- The scan never waits for files to finish saving.
- Saved files get a ✓ in the **Saved** column.
- If you press **Stop**, or the drive disconnects, everything found up to that point is still saved. This makes it the best option for a failing drive.
- If the destination fills up, saving stops with a warning and the scan keeps going.
- Each scan saves into its own `FlashRescue <date time>` folder.
- If you later use **Recover selected…**, it asks whether to skip files that were already auto-saved.
- Your folder and type choices are remembered for next time.

### What "Condition" means

| Condition | Meaning |
|---|---|
| Good | The data is in the right place and starts the way that file type should. It will very likely open. |
| Fragmented (best guess) | Something now sits in the middle of the old space, so FlashRescue skipped over it. This often works. |
| Possibly overwritten | The data no longer starts the way the file type should. Probably damaged. |
| Overwritten / Partially overwritten | A newer file is using that space. Usually unrecoverable. |
| Erased (all zeros) | The space was wiped. This is normal on SSDs, which clear deleted space automatically (TRIM). |
| Compressed / Encrypted (not supported) | An NTFS-compressed or EFS-encrypted file. Its raw data can't be turned back into the file. |
| Incomplete record | Part of the file's location map was lost. The missing part is filled with zeros. |
| Carved | Found by deep scan. It's intact if the file was stored in one piece, which is usually true on flash drives and camera cards. |

Files in the unrecoverable rows are hidden unless you untick **Hide unrecoverable**. Auto-recover skips them unless you untick **Skip unrecoverable files**.

### NTFS notes

- Deleted files come back with their full name, folder path and date, including files inside deleted folders. Fragmented files are fine too, because NTFS keeps each file's location map after deletion.
- Very small files (under about 700 bytes) live inside the file table itself, and are recovered from there.
- If a file's name record was reused but its location map survived, it's listed under `/(name lost)/File record 1234.jpg`. The extension is guessed from the content.
- If the folder a file was in has been reused, the file goes under `/(lost folder)/…`.

### NAS and RAID drives (Virtual RAID…)

Use this for drives from a Synology, QNAP or other NAS, from a RAID card, or from motherboard RAID (Intel RST). Each drive holds only a slice of the data, so scanning one drive by itself gives you fragments. **Virtual RAID…** puts the array back together in memory. **Nothing is written to the drives.**

1. Connect every member drive. Click **Virtual RAID…** and add each drive. You can add rescue images instead. If a drive is failing, make a rescue image of it first and add the image. If a drive is dead or gone, add a **missing drive** in its place.
2. **NAS / Linux RAID:** click **Read settings from the drives**. The drives store the array settings, so the drive order doesn't matter. On Synology and QNAP, the data sits in an LVM volume inside the array (for example `vg1000/lv`). Pick that line in the results.
3. **RAID card / motherboard RAID:** the settings live on the controller, not the drives. Click **Work out settings**. FlashRescue tries every drive order, stripe size and layout (RAID 0/1/5, up to 6 drives) and scores each one by whether the filesystem makes sense. The best match is listed first. If you know the settings, enter them and click **Build with these settings**. That also covers RAID 6, RAID 10 and spanned (JBOD) arrays.
4. Click **Use selected**. The array appears in the drive list, and you scan it like any other drive.

- RAID 5 can rebuild one missing drive. RAID 6 can rebuild any two. Unreadable sectors on a member, including gaps in a rescue image's map, are rebuilt from parity the same way.
- A single drive from a two-drive Synology mirror (SHR or RAID 1) doesn't need this. Scan it directly.

### ext4 and btrfs notes (Linux / NAS drives)

- **ext4 / ext3 / ext2:** live files are listed with their real names and folders. When a file is deleted, ext4 wipes its location map, so FlashRescue looks for older copies in the filesystem journal. Recent deletions often come back that way, with full names. If a deleted name is found but no copy of its map is left, the summary counts it, and deep scan is the fallback.
- **btrfs** (Synology's default): btrfs never overwrites in place, so old versions of its file tree often survive. FlashRescue searches them, and the backup trees, for deleted files with their names. Files that exist only in a snapshot are listed as **Snapshot**. A deep scan also finds orphaned old tree blocks anywhere on the volume. zlib-compressed files are supported; LZO and zstd compressed files aren't.
- Lost ext partitions are found even when the main superblock is wiped, using the backup copies.

## Deep scan file types

Photos: JPG, PNG, GIF, BMP, WEBP, TIFF, HEIC/AVIF, CR2, CR3, NEF, ARW, DNG, PEF
Video: MP4, MOV, M4V, 3GP, AVI, MKV, WEBM
Audio: MP3 (with ID3 tag), WAV, M4A
Documents: PDF, DOCX, XLSX, PPTX, DOC, XLS, PPT, ODT/ODS/ODP, EPUB
Archives: ZIP, 7Z, APK, JAR

## Limits

- On FAT drives, files with short names like `report.pdf` lose their first letter when deleted, so they show up as `_eport.pdf`. Long names are recovered in full.
- Nothing can recover data that has been overwritten, or a drive that was fully formatted (not quick-formatted).
- **SSDs:** Windows tells SSDs which space is free (TRIM), and the SSD wipes it, often within minutes. Recovery from an internal SSD usually only works right after deleting. Hard drives, USB sticks and memory cards are much better candidates.
- ReFS, XFS and Mac filesystems (APFS, HFS+) aren't read directly, but deep scan works on any drive.
- A deep scan of a large hard drive takes a while: about 2 hours per TB at 150 MB/s. Leave **Only search free space** ticked unless the drive was formatted.
- Plain-text files (`.txt`, `.csv`) have no signature, so deep scan can't find them. The fast scan can.

## Tip: scan an image instead of the drive

If the drive is failing, first copy it to an image file with a tool like HDD Raw Copy or Win32 Disk Imager. Then use **Open disk image file…** to scan the image as many times as you like without touching the drive again.

## Files

- `src/FlashRescue.py`: the app window
- `src/engine.py`: recovery engine (NTFS/FAT/exFAT reading, partition tables, deep scan, saving files)
- `src/rescue.py`: rescue imaging (passes, map file, timeouts, reconnect, cool-down)
- `src/health.py`: drive health (SMART for SATA/ATA and NVMe)
- `src/extras.py`: file tables from before a format, lost partitions, Recycle Bin, thumbnail caches, photo dates, saved results, duplicates
- `src/media.py`: Check files and Repair video (uses ffmpeg)
- `tools/`: ffmpeg.exe and ffprobe.exe (downloaded by Setup.bat)
