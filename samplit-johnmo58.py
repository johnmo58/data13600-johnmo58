# Target branch: randomly print roughly 1 in 100 lines
import sys
import random

filename = sys.argv[1]

with open(filename) as f:
    for line in f:
        if random.random() < 0.01:
            print(line, end="")
            