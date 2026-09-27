---
name: csv-totals
description: House style for CSV column totals. Use whenever a task asks to sum, total, or aggregate a column of a CSV file.
---

# CSV totals (house style)

When you write or fix code that totals a CSV column, follow these rules.

1. **Parse with the `csv` module.** Never split lines on commas: quoted fields
   contain commas (`"first, with comma"`), and naive splitting shifts columns.
2. **Use `decimal.Decimal` for money.** Never accumulate money in `float`;
   `0.1 + 0.2 != 0.3` and the total comes out wrong.
3. **Print the plain decimal.** `python sum_csv.py <file> <column>` prints only
   the total, as a plain decimal (`12.50`), nothing else.

Reference shape:

```python
import csv
import sys
from decimal import Decimal

def total(path: str, column: str) -> Decimal:
    acc = Decimal("0")
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            acc += Decimal(row[column])
    return acc

if __name__ == "__main__":
    print(total(sys.argv[1], sys.argv[2]))
```
