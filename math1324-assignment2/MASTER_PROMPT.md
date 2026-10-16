# Master prompt — MATH1324 Assignment 2

Copy everything inside the block below into an AI assistant. Attach the course's R Markdown template, and fill in the `[brackets]` first.

```text
You are an expert statistician and R Markdown author helping me with MATH1324 (Introduction to
Statistics) Assignment 2. It is worth 25% and is marked on a rubric (below). Produce a complete,
reproducible R Markdown slide deck that knits to HTML (slidy_presentation, from the template I
attached) and that I can then save as a PDF.

MY DETAILS
- Name / student ID: [NAME, sXXXXXXX]
- Topic or dataset I'm interested in (optional): [e.g. "income vs CO2 emissions" or "you choose"]

HARD REQUIREMENTS
1. Use OPEN data, ideally Creative Commons (e.g. Our World in Data, World Bank, UCI, data.gov,
   ABS, data.vic.gov.au, BOM, Kaggle with an open licence). State the licence, the URL and the
   download date. Explain how the provider collected the data and the sampling method (e.g. a
   census of countries vs a random sample), so someone else could replicate it.
2. Pose ONE interesting real-world question with a strong rationale, so the reader wants to know
   the answer. Break it into 2–3 specific research questions, each matched to a method.
3. Do exactly TWO of these three tasks (default: 5 and 7):
   5) Hypothesis test + confidence interval (e.g. paired t-test, Welch two-sample t-test).
   6) Categorical association (chi-square test of association).
   7) Linear regression.
   For each: state H0/HA in LaTeX, check EVERY assumption with a test or plot (normality/CLT,
   independence, Levene's test for equal variances, linearity, homoscedasticity, Q-Q plot of
   residuals, Cook's distance), report the test statistic, df, p-value and 95% CI, make a decision
   at alpha = 0.05, and interpret it in plain language in context.
4. A maximum of 22 slides, including the title slide. Each slide must fit on one screen with no
   scrolling. Use concise bullets. Use plain, short sentences that are easy to follow.
5. Required sections, in this order: Title (name/ID in the YAML) → Introduction (background,
   key terms defined) → Problem Statement (table of RQs and methods) → Data (source, licence,
   collection, sampling; table of variables with type/scale/units and factor levels;
   pre-processing code with a justification for every step, including missing data) →
   Descriptive Statistics & Visualisation (summary table with Min/Q1/Median/Q3/Max/Mean/SD/n/
   Missing via knitr::kable; 3–4 ggplot2 figures that tell the story; how outliers and skew were
   handled, e.g. a log transform) → the two analysis tasks (assumptions slide + results slide
   each) → Discussion (major findings tied back to the RQs, strengths, limitations, future work,
   ONE bold take-home message) → References (APA 7th, including the dataset, papers cited and R
   packages).
6. Every number in the text must come from inline R code (`r ...`), never typed by hand.
   Re-read every interpretation against the actual output; if a robustness check agrees or
   disagrees, say so accurately.
7. Show the R code and output (echo = TRUE). Load packages quietly in the setup chunk. Save a
   small snapshot of the data next to the .Rmd and read it with a relative path, so the
   document knits offline.
8. Add sensitivity checks that make the analysis convincing: a non-parametric equivalent
   (Wilcoxon), re-running without extreme outliers, and/or a quadratic term in the regression.

RUBRIC (aim for full marks)
- Introduction/problem (2): a compelling rationale, important concepts defined.
- Data & descriptives (4): clear, concise, detailed; shows an in-depth knowledge of the data.
- Methodology & analysis (10): analysis justified, assumptions checked, results reported and
  interpreted clearly and accurately.
- Discussion (6): critical analysis, a conclusion that answers the question, a succinct
  take-home message.
- Written presentation (3): professional, uncrowded, no spelling errors, proper referencing.

DELIVERABLES
- The complete .Rmd file, ready to knit, in one code block.
- Then a short checklist: slide count, which tasks were used, and anything I must change
  (my name/ID, file paths).
- Tell me how to export to PDF: knit to HTML, open it in Chrome, Print → Save as PDF,
  landscape, background graphics on.
```
