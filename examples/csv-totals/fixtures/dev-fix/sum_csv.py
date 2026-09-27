"""Print the total of a CSV column. Usage: python sum_csv.py <file> <column>"""
import sys


def total(path, column):
    lines = open(path).read().splitlines()
    header = lines[0].split(",")
    idx = header.index(column)
    acc = 0.0
    for line in lines[1:]:
        acc += float(line.split(",")[idx])
    return acc


if __name__ == "__main__":
    print(total(sys.argv[1], sys.argv[2]))
