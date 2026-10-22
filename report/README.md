# GAIA capstone report (LaTeX, Overleaf-ready)

Report for the PCCOE B.Tech MDM Capstone Project (Generative AI), written to the department's format
instructions: A4 with the required margins, Times 12 pt, 1.5 spacing, bold centred 14 pt capital chapter
titles, 12 pt bold section headings, the centred 10 pt footer with the page number below it, Roman page
numbers up to the table of contents and Arabic numbers from the Introduction, figure captions below and table
captions above, IEEE references in order of first citation.

## Open it in Overleaf

1. Overleaf, **New Project, Upload Project**, choose `GAIA_report_overleaf.zip`.
2. Compiler: **pdfLaTeX** (default). Main document: `main.tex`. It also compiles with XeLaTeX or Tectonic.
3. Recompile twice after edits so the table of contents, lists and references update.

## What you must fill in (everything is highlighted in yellow while `\drafttrue` is set)

Edit the **"EDIT HERE"** block at the top of `main.tex`:

| Macro | What |
|---|---|
| `\GroupID` | group number |
| `\StudentA` ... `\StudentD`, `\PRNA` ... `\PRND` | names and PRNs |
| `\GuideName`, `\GuideProf` | guide's name |
| `\CertDate` | date on the certificate |

Other places marked in yellow: the acknowledgement names, the assumed cell capacity in Section 4.4, the
hardware test results in Section 6.1.3, the Gemini evaluation columns/tables in Chapter 7 (commands are given
there), and Appendix A (paper) and Appendix B (plagiarism report).

Before printing, set `\draftfalse` in `main.tex` (removes the yellow highlighting).

## Images to add (drop them into `figures/`; empty frames are shown until then)

| File | Where it appears |
|---|---|
| `pccoe_logo.png` | cover page and certificate |
| `rover_front.jpg`, `rover_top.jpg` | Figure 7.8 (assembled rover) |
| `rover_arm.jpg`, `rover_field.jpg` | Figure 7.9 (soil-probe arm, field trial) |
| `app_live.png`, `app_switches.png` | Figure 7.6 (Live view, sensor switches) |
| `app_chat_stream.png`, `app_gap_answer.png` | Figure 7.7 (streamed chat, gap explanation) |

The existing screenshots in `figures/` are grey-scale copies from the project's `docs/screenshots`; they were
captured before the sensor switches, streaming and Live view were added, so replace them if you can.
Black-and-white, sharp images with at most two per page are what the format asks for.

## Structure

```
main.tex                 preamble, formatting rules, identity macros, document order
frontmatter/             cover, certificate, acknowledgement, abstract, abbreviations, nomenclature
chapters/ch01..ch08      the eight chapters of the department's table of contents
chapters/appendices.tex  Appendix A (paper), B (plagiarism report), C (API summary)
figures/diagrams/*.tex   all diagrams as TikZ (editable text, black and white)
figures/*.png            screenshots
references.bib           IEEE references (BibTeX, IEEEtran)
```

## Honesty notes

* Results in Chapters 6 and 7 are numbers the project actually produced: 96 backend and 22 application tests,
  the ten-question evaluation (offline and local-model runs), and the performance measurements on the
  i5-8365U laptop. The vision benchmark with the Gemini model and the Gemini RAG evaluation had not been run,
  so those cells are empty placeholders. Do not fill them with numbers you have not measured.
* The project timeline (Table 1.1, Figure 1.3) is indicative: change the weeks to your real schedule.
* Every reference was checked against its source (arXiv or publisher page) before it was added.
* Related-systems tables only claim what the cited publications report; a dash means "not claimed".
