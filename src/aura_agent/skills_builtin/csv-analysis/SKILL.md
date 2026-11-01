---
name: csv-analysis
description: Analyse a CSV or spreadsheet export and report findings.
license: Apache-2.0
---
1. Look at the shape first: columns, row count, types, missing values (a short Python script with the csv module or pandas if installed).
2. Answer the user's question with the smallest correct computation; show the code you ran.
3. Check results for sanity (totals add up, no silent type coercion, dates parsed right).
4. Report numbers with their units and the rows they came from; save charts or outputs to files the user can open.
