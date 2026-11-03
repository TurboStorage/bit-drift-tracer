# Bit Drift Tracer

![Bit Drift Tracer](./Bit%20Drift%20Tracer.png)

Traces host-visible file read throughput against host/filesystem timestamps. It is useful for finding correlations, outliers, and changes in the performance visible to the host, but it does not directly measure NAND media state.

The timestamp-derived X axis is an observation-time proxy: the tool uses the earlier of a file's modification and creation/change timestamps as reported by the host filesystem. Those timestamps can be copied, restored, rewritten, or defined differently by different filesystems. They do not reveal when the SSD programmed the underlying NAND.

---

## What It Does

- Scans a selected drive or folder and reads each file using Windows unbuffered I/O (`FILE_FLAG_NO_BUFFERING`) when available, reducing normal OS page-cache effects
- Reads from a **random offset** within each sufficiently large file to reduce systematic bias toward file beginnings
- Uses `FILE_FLAG_RANDOM_ACCESS` to suppress storage-driver prefetch hints
- Records read speed (MB/s), timestamp-derived observed age, and file size for every file
- Plots the results as a scatter graph (speed vs. observed age) with color-coded timestamp brackets, a linear trend line, and a rolling average
- Shows average speed per timestamp bracket as a bar chart below the scatter

---

## Requirements

- Windows 10 or 11 (64-bit)
- Python 3.10 or newer — [python.org](https://www.python.org/downloads/)
- `matplotlib` and `numpy` — install once with:

```
pip install -r requirements.txt
```

---

## Usage

Double-click `Bit Drift Tracer.bat` to launch (it starts the tool without a console window). `Bit Drift Tracer.py` also runs directly.

| Control | Description |
|---|---|
| **Target** | Path to a drive root (`D:\`) or any folder |
| **Browse…** | Folder picker dialog |
| **Min size (MB)** | Skip files smaller than this — default 0.5 MB |
| **Randomize order** | Shuffle file order before measuring (collects all paths first, then shuffles) |
| **▶ Scan** | Start scanning |
| **Log age axis and fit** | Switch the scatter to a log age axis (1 week to 8 years) and fit speed against log age instead of weeks |
| **■ Stop** | Stop at any time — results already collected are kept |
| **Export TSV** | Save results as a tab-separated file (Age / Rate / Size / Path) |
| **Export PNG** | Save the current graph as a PNG image |

---

## How to Read the Graph

**Scatter plot (top)**
Each dot is one file. The X axis is age derived from host/filesystem timestamps; the Y axis is host-visible read speed in MB/s.

| Color | Age range |
|---|---|
| Green | Less than 3 months |
| Blue | 3 months – 1 year |
| Yellow | 1 – 2 years |
| Orange | 2 – 4 years |
| Red | 4+ years |

The **dashed white line** is a linear trend fit across all data. Its slope describes correlation in this sample only. A downward slope means files with earlier timestamps were read more slowly during the scan; it does not identify the cause.

**Log age axis and fit.** Tick the box in the controls row to plot age on a log scale and fit a straight line to read speed against log10(1 + age in days). The slope is reported in MB/s per decade of age (per tenfold increase). The reason for the option: in published 3D NAND retention measurements, the threshold-voltage shift is linear in the logarithm of retention time (Luo et al., HeatWatch, HPCA 2018, Equation 2), so loss is fast early and slow later, and a straight line in weeks can fit that shape poorly. The line under the controls shows R-squared for both fits on your data so you can compare shapes. File age is not retention time: dwell time before the data was last rewritten and the temperature history of the drive also change how fast charge is lost, and file timestamps can be copied or reset.

The **solid blue line** is a rolling average that smooths out the scatter noise.

**Bar chart (bottom)**
Average read speed per timestamp bracket side by side.

---

## Why Random Offset Reads

Reading only from offset 0 can introduce a repeatable location bias because file beginnings may receive different host access patterns:

- Windows Explorer reads file headers for metadata and thumbnails
- Media players read the first few MB on open
- Windows Search indexes file beginnings
- Other software may have accessed those regions recently

The tool therefore picks a random sector-aligned position inside each sufficiently large file. `FILE_FLAG_NO_BUFFERING` and the random-access hint can reduce OS buffering and prefetch effects, but they cannot guarantee bypass of storage-driver, bridge, SSD-controller, SLC, or DRAM caches. A random logical offset also does not prove a direct NAND read or expose physical NAND placement.

---

## Methodology Limits

Results are affected by file size and layout, queueing, background activity, thermal and power states, filesystem behavior, fragmentation, compression, encryption, controller firmware, logical-to-physical remapping, and multiple cache layers. Repeat scans and controlled comparisons are more informative than a single trend line.

The tool cannot infer NAND cell age, program/erase wear, retention state, ECC correction effort, refresh history, or physical placement from filesystem timestamps and read timing. Claims about those internal states require device telemetry, vendor diagnostics, controlled experiments, or other independent evidence.

Use Bit Drift Tracer as a host-visible data-stability and read-performance tracer: it can reveal repeatable throughput differences worth investigating, without assigning those differences to a specific NAND mechanism.
