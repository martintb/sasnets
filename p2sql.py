"""
Utility script that uploads data files to a python script.

Reads files generated by sasmodels and uploads the individual data sessions
to a postgres database running on localhost. Slow.
"""
from __future__ import print_function

import argparse
import ast
import os
import re
import sys

import psycopg2 as pgsql

parser = argparse.ArgumentParser()
parser.add_argument("key", help="DB Table identifier")
parser.add_argument("-c", "--create", help="Create new db", action="store_true")
parser.add_argument("path", help="Relative or absolute path to a folder "
                                 "containing data files")


# noinspection SqlNoDataSourceInspection
def main(args):
    """
    Main function. Args should conform to the argparse args specified.
    :param args: Arguments from the command line
    :return: None
    """
    parsed = parser.parse_args(args)
    conn = pgsql.connect(
        "dbname=sas_data user=sasnets password=sasnets host=127.0.0.1")
    c = conn.cursor()
    if parsed.create:
        c.execute("CREATE TABLE data_" + parsed.key +
                  "(Name TEXT NOT NULL, Num INTEGER NOT NULL, Q TEXT NOT NULL, "
                  "IQ TEXT NOT NULL)")
    path = parsed.path
    nlines = 0
    pattern = re.compile("all")
    for fn in sorted(os.listdir(path)):
        if pattern.search(fn):
            try:
                with open(path + fn, 'r') as fd:
                    iq_list, y_list = (list() for i in range(2))
                    print("Reading " + fn)
                    templ = ast.literal_eval(fd.readline().strip())
                    y_list.extend([templ[0] for i in range(templ[1])])
                    ast.literal_eval(fd.readline().strip())
                    iq_list.extend(ast.literal_eval(fd.readline().strip()))
                    nlines += templ[1]
                    for iq, y in zip(iq_list, y_list):
                        c.execute(
                            "INSERT INTO train_data (iq, model) VALUES (%S, %S)",
                            (iq, y))
                    del iq_list
                    del y_list
            except:
                raise
    conn.commit()
    c.close()
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
