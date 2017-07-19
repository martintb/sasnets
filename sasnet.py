from __future__ import print_function

import argparse
import ast
import itertools
import logging
import multiprocessing
import os
import re
import sys
import time

import keras
import matplotlib.pyplot as plt
import numpy as np
import psycopg2 as psql
import ruamel.yaml as yaml  # using ruamel for better input processing.
from keras.callbacks import TensorBoard, EarlyStopping
from keras.layers import Conv1D, Dropout, Flatten, Dense, \
    Embedding, MaxPooling1D
from keras.models import Sequential
from keras.utils.np_utils import to_categorical
from psycopg2 import sql
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder

parser = argparse.ArgumentParser(
    description="Use neural nets to classify scattering data.")
parser.add_argument("path", help="Relative or absolute path to a folder "
                                 "containing data files")
parser.add_argument("-v", "--verbose", help="Control output verbosity",
                    action="store_true")
parser.add_argument("-s", "--save-path",
                    help="Path to save model weights and info to")

gpath = ""
gpattern = ""

DEC2FLOAT = psql.extensions.new_type( # May not be working
    psql._psycopg.DECIMAL.values,
    'DEC2FLOAT',
    lambda value, curs: float(value) if value is not None else None)
psql.extensions.register_type(DEC2FLOAT, None)


def sql_dat_gen(dname, mname, dbname="sas_data", host="127.0.0.1", port="5673",
                user="sasnets", encoder=None):
    conn = psql.connect("dbname=" + dbname + " user=" + user + " host=" + host)
    with conn:
        with conn.cursor() as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS tsm_system_rows")
            c.execute(
                sql.SQL("SELECT * FROM {}").format(
                    sql.Identifier(mname)))
            x = np.asarray(c.fetchall())
            # pprint(x)
            q = x[0][1]
            dq = x[0][2]
            diq = x[0][3]
            while True:
                c.execute(
                    sql.SQL(
                        "SELECT * FROM {} TABLESAMPLE SYSTEM_ROWS(5)").format(
                        sql.Identifier(dname)))
                x = np.asarray(c.fetchall())
                iq_list = x[:, 1]
                y_list = x[:, 2]
                encoded = encoder.transform(y_list)
                yt = np.asarray(to_categorical(encoded, 71))
                q_list = np.asarray(
                    [np.transpose([q, iq, dq, diq]) for iq in iq_list])
                yield q_list, yt
    conn.close()


def sql_net(dn, mn, verbosity=False, save_path=None, encoder=None, xval=None, yval=None):
    if verbosity:
        v = 1
    else:
        v = 0
    base = None
    sp = os.path.normpath(save_path)
    if sp is not None:
        if os.path.isdir(sp):
            base = os.path.join(sp, str(time.time()))
        else:
            base = sp
        if not os.path.exists(os.path.dirname(sp)):
            os.makedirs(os.path.dirname(sp))
    tb = TensorBoard(log_dir=os.path.dirname(base), histogram_freq=1)
    es = EarlyStopping(min_delta=0.005, patience=5, verbose=v)

    # Begin model definitions
    model = Sequential()
    # model.add(Embedding(4000, 128, input_length=267))
    model.add(Conv1D(256, kernel_size=8, activation='relu', input_dim=4,
                     input_length=267))
    model.add(MaxPooling1D(pool_size=4))
    model.add(Dropout(.17676))
    model.add(Conv1D(256, kernel_size=6, activation='relu'))
    model.add(MaxPooling1D(pool_size=3))
    model.add(Dropout(.20782))
    model.add(Flatten())
    model.add(Dense(64, activation='tanh'))
    model.add(Dropout(.20582))
    model.add(Dense(71, activation='softmax'))
    model.compile(loss="categorical_crossentropy",
                  optimizer=keras.optimizers.Adadelta(),
                  metrics=['accuracy'])

    # Model Run
    if v:
        print(model.summary())
    history = model.fit_generator(sql_dat_gen(dn, mn, encoder=encoder), 20000,
                                  epochs=60, workers=1, verbose=v, validation_data=(xval, yval),
                                  max_queue_size=1, callbacks=[tb, es])
    score = None

    # Model Save
    plt.plot(history.history['acc'])
    plt.plot(history.history['val_acc'])
    plt.title('model accuracy')
    plt.ylabel('accuracy')
    plt.xlabel('epoch')
    plt.legend(['train', 'test'], loc='upper left')
    if not (base is None):
        with open(base + ".history", 'w') as fd:
            fd.write(str(history.history) + "\n")
            if score is not None:
                fd.write(str(score) + "\n")
        model.save(base + ".h5")
        with open(base + ".svg", 'w') as fd:
            plt.savefig(fd, format='svg', bbox_inches='tight')
    if xval is not None and yval is not None:
        score = model.evaluate_generator((xval, yval), 2000)
        print('\nTest loss: ', score[0])
        print('Test accuracy:', score[1])
    logging.info("Complete.")



# noinspection PyUnusedLocal
def read_parallel_1d(path, pattern='_eval_', typef='aggr', verbosity=False):
    """
    Reads all files in the folder path. Opens the files whose names match the
    regex pattern. Returns lists of Q, I(Q), and ID. Path can be a
    relative or absolute path. Uses Pool and map to speed up IO. WIP. Uses an
    excessive amount of memory currently. It is recommended to use sequential on
    systems with less than 16 GiB of memory.

    Calling parallel on 69 150k line files, a gc, and parallel on 69 5k line
    files takes around 70 seconds. Running sequential on both sets without a gc
    takes around 562 seconds. Parallel peaks at 15 + GB of memory used with two
    file reading threads. Sequential peaks at around 7 to 10 GB.

    typef is one of 'json' or 'aggr'. JSON mode reads in all and only json files
    in the folder specified by path. aggr mode reads in aggregated data files.
    See sasmodels/generate_sets.py for more about these formats.

    Assumes files contain 1D data.

    :type path: String
    :type pattern: String
    :type typef: String
    :type verbosity: Boolean
    """
    global gpath
    global gpattern
    q_list, dq_list, iq_list, diq_list, y_list = (list() for i in range(5))
    # pattern = re.compile(pattern)
    n = 0
    nlines = None
    if typef == 'json':
        for fn in os.listdir(path):
            if pattern.search(fn):  # Only open JSON files
                with open(path + fn, 'r') as fd:
                    n += 1
                    data_d = yaml.safe_load(fd)
                    q_list.append(data_d['data']['Q'])
                    iq_list.append(data_d["data"]["I(Q)"])
                    y_list.append(data_d["model"])
                if (n % 100 == 0) and verbosity:
                    print("Read " + str(n) + " files.")
    if typef == 'aggr':
        gpattern = pattern
        gpath = path
        nlines = 0
        l = 0
        fn = os.listdir(path)
        chunked = [fn[i: i + 1] for i in xrange(0, len(fn), 1)]
        pool = multiprocessing.Pool(multiprocessing.cpu_count() - 6,
                                    maxtasksperchild=2)
        result = np.asarray(
            pool.map(read_h, chunked, chunksize=1))
        pool.close()
        pool.join()
        logging.info("IO Done")
        result = list(itertools.chain.from_iterable(result))
        q_list = result[0::3]
        iq_list = result[1::3]
        y_list = result[2::3]
    else:
        print("Error: the type " + typef + " was not recognised. Valid types "
                                           "are 'aggr' and 'json'.")
        return None
    return q_list, iq_list, y_list, nlines


def read_h(l):
    logging.info(os.getpid())
    if l is None:
        raise Exception("Empty args")
    global gpath
    global gpattern
    q_list, iq_list, y_list = (list() for i in range(3))
    p = re.compile(gpattern)
    for fn in l:
        if p.search(fn):
            try:
                with open(gpath + fn, 'r') as fd:
                    logging.info("Reading " + fn)
                    templ = ast.literal_eval(fd.readline().strip())
                    y_list.extend([templ[0] for i in range(templ[1])])
                    t2 = ast.literal_eval(fd.readline().strip())
                    q_list.extend([t2 for i in range(templ[1])])
                    iq_list.extend(ast.literal_eval(fd.readline().strip()))
            except Exception as e:
                logging.warning("skipped, " + str(e))
    return q_list, iq_list, y_list


# noinspection PyCompatibility,PyUnusedLocal
def read_seq_1d(path, pattern='_eval_', typef='aggr', verbosity=False):
    """
    Reads all files in the folder path. Opens the files whose names match the
    regex pattern. Returns lists of Q, I(Q), and ID. Path can be a
    relative or absolute path. Uses a single thread only. It is recommended to
    use :meth:`read_parallel_1d`, except in hyperopt, where map() is broken.

    typef is one of 'json' or 'aggr'. JSON mode reads in all and only json files
    in the folder specified by path. aggr mode reads in aggregated data files.
    See sasmodels/generate_sets.py for more about these formats.

    Assumes files contain 1D data.

    :type path: String
    :type pattern: String
    :type typef: String
    :type verbosity: Boolean
    """
    q_list, dq_list, iq_list, diq_list, y_list = (list() for i in range(5))
    pattern = re.compile(pattern)
    n = 0
    nlines = None
    if typef == 'json':
        for fn in os.listdir(path):
            if pattern.search(fn):  # Only open JSON files
                with open(path + fn, 'r') as fd:
                    n += 1
                    data_d = yaml.safe_load(fd)
                    q_list.append(data_d['data']['Q'])
                    iq_list.append(data_d["data"]["I(Q)"])
                    y_list.append(data_d["model"])
                if (n % 100 == 0) and verbosity:
                    print("Read " + str(n) + " files.")
    if typef == 'aggr':
        nlines = 0
        for fn in sorted(os.listdir(path)):
            if pattern.search(fn):
                try:
                    with open(path + fn, 'r') as fd:
                        print("Reading " + fn)
                        templ = ast.literal_eval(fd.readline().strip())
                        y_list.extend([templ[0] for i in xrange(templ[1])])
                        t2 = ast.literal_eval(fd.readline().strip())
                        q_list.extend([t2 for i in xrange(templ[1])])
                        iq_list.extend(ast.literal_eval(fd.readline().strip()))
                        dqt = ast.literal_eval(fd.readline().strip())
                        dq_list.extend([dqt for i in xrange(templ[1])])
                        diqt = ast.literal_eval(fd.readline().strip())
                        diq_list.extend([diqt for i in xrange(templ[1])])
                        nlines += templ[1]
                    if (n % 1000 == 0) and verbosity:
                        print("Read " + str(nlines) + " points.")
                except Exception as e:
                    logging.warning("skipped, " + str(e))
    else:
        print("Error: the type " + typef + " was not recognised. Valid types "
                                           "are 'aggr' and 'json'.")
    return q_list, iq_list, y_list, dq_list, diq_list, nlines


def plot(q, i_q):
    """
    Method to plot Q vs I(Q) data for testing and verification purposes.

    :param q: List of Q values
    :param i_q: List of I values
    :return: None
    """
    plt.style.use("classic")
    plt.plot(q, i_q)
    ax = plt.gca()
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.autoscale(enable=True)
    plt.show()


def oned_convnet(x, y, xevl=None, yevl=None, random_s=235, verbosity=False,
                 save_path=None):
    """
    Runs a 1D convolutional classification neural net on the input data x and y.

    :param x: List of training data x
    :param y: List of corresponding categories for each vector in x
    :param xevl: List of evaluation data
    :param yevl: List of corresponding categories for each vector in x
    :param random_s: Random seed. Defaults to 235 for reproducibility purposes, but should be set randomly in an actual run.
    :param verbosity: Either true or false. Controls level of output.
    :param save_path: The path to save the model to. If it points to a directory, writes to a file named the current unix time. If it points to a file, the file is overwritten.
    :return: None
    """
    if verbosity:
        v = 1
    else:
        v = 0
    base = None
    sp = os.path.normpath(save_path)
    if sp is not None:
        if os.path.isdir(sp):
            base = os.path.join(sp, str(time.time()))
        else:
            base = sp
        if not os.path.exists(os.path.dirname(sp)):
            os.makedirs(os.path.dirname(sp))
    encoder = LabelEncoder()
    encoder.fit(y)
    encoded = encoder.transform(y)
    yt = to_categorical(encoded)
    xval, xtest, yval, ytest = train_test_split(x, yt, test_size=.25,
                                                random_state=random_s)
    if not len(set(y)) == len(set(yevl)):
        raise ValueError("Differing number of categories in train (" + str(
            len(set(y))) + ") and test (" + str(len(set(yevl))) + ") data.")
    tb = TensorBoard(log_dir=os.path.dirname(base), histogram_freq=1)
    es = EarlyStopping(min_delta=0.005, patience=5, verbose=v)

    # Begin model definitions
    model = Sequential()
    model.add(Embedding(4000, 128, input_length=xval.shape[1]))
    model.add(Conv1D(128, kernel_size=6, activation='relu'))
    model.add(MaxPooling1D(pool_size=4))
    model.add(Dropout(.17676))
    model.add(Conv1D(64, kernel_size=6, activation='relu'))
    model.add(MaxPooling1D(pool_size=4))
    model.add(Dropout(.20782))
    model.add(Flatten())
    model.add(Dense(32, activation='tanh'))
    model.add(Dropout(.20582))
    model.add(Dense(len(set(y)), activation='softmax'))
    if len(set(y)) == 2:
        l = 'binary_crossentropy'
    else:
        l = 'categorical_crossentropy'
    model.compile(loss=l, optimizer=keras.optimizers.Adadelta(),
                  metrics=['accuracy'])
    # plot_model(model, to_file="model.png")

    # Model Run
    if v:
        print(model.summary())
    history = model.fit(xval, yval, batch_size=5, epochs=50, verbose=v,
                        validation_data=(xtest, ytest), callbacks=[tb, es])
    score = None
    if not (xevl is None) and not (yevl is None):
        e2 = LabelEncoder()
        e2.fit(yevl)
        yv = to_categorical(e2.transform(yevl))
        score = model.evaluate(xevl, yv, verbose=v)
        print('\nTest loss: ', score[0])
        print('Test accuracy:', score[1])

    # Model Save
    plt.plot(history.history['acc'])
    plt.plot(history.history['val_acc'])
    plt.title('model accuracy')
    plt.ylabel('accuracy')
    plt.xlabel('epoch')
    plt.legend(['train', 'test'], loc='upper left')
    if not (base is None):
        with open(base + ".history", 'w') as fd:
            fd.write(str(list(set(y))) + "\n")
            fd.write(str(history.history) + "\n")
            if score is not None:
                fd.write(str(score) + "\n")
            fd.write("Seed " + str(random_s))
        model.save(base + ".h5")
        with open(base + ".svg", 'w') as fd:
            plt.savefig(fd, format='svg', bbox_inches='tight')
    logging.info("Complete.")


def trad_nn(x, y, xevl=None, yevl=None, random_s=235):
    """
    Runs a traditional MLP categorisation neural net on the input data x and y.

    :param x: List of training data x
    :param y: List of corresponding categories for each vector in x
    :param random_s: Random seed. Defaults to 235 for reproducibility purposes, but should be set randomly in an actual run.
    :param xevl: Evaluation data for model
    :param yevl: Evaluation data for model
    :return: None
    """
    encoder = LabelEncoder()
    encoder.fit(y)
    encoded = encoder.transform(y)
    yt = to_categorical(encoded)
    xval, xtest, yval, ytest = train_test_split(x, yt, test_size=.25,
                                                random_state=random_s)
    model = Sequential()
    model.add(Dense(128, activation='relu', input_dim=x.shape[1]))
    model.add(Dropout(0.25))
    model.add(Dense(256, activation='relu'))
    model.add(Dropout(0.5))
    model.add(Dense(256, activation='relu'))
    model.add(Dropout(0.25))
    model.add(Dense(512, activation='relu'))
    model.add(Dropout(0.5))
    model.add(Dense(len(set(y)), activation='softmax'))
    model.compile(loss='categorical_crossentropy', optimizer="adam",
                  metrics=['accuracy'])
    print(model.summary())
    history = model.fit(xval, yval, batch_size=10, epochs=10, verbose=1,
                        validation_data=(xtest, ytest))
    if xevl and yevl:
        score = model.evaluate(xtest, ytest, verbose=0)
        print('Test loss: ', score[0])
        print('Test accuracy:', score[1])


def main(args):
    parsed = parser.parse_args(args)
    # time_start = time.clock()
    # a, b, c, d, e, n = read_seq_1d(parsed.path, pattern='_all_',
    #                               verbosity=parsed.verbose)
    # gc.collect()
    # at, bt, ct, dt, et, nt = read_seq_1d(parsed.path, pattern='_eval_',
    #                                     verbosity=parsed.verbose)
    # time_end = time.clock() - time_start
    # logging.info("File I/O Took " + str(time_end) + " seconds for " + str(n) +
    #             " points of data.")
    # r = random.randint(0, 2 ** 32 - 1)
    # logging.warn("Random seed for this iter is " + str(r))
    # oned_convnet(np.asarray(b), c, np.asarray(bt), ct, random_s=r,
    #             verbosity=parsed.verbose, save_path=parsed.save_path)
    conn = psql.connect("dbname=sas_data user=sasnets host=127.0.0.1")
    # conn.set_session(readonly=True)
    with conn:
        with conn.cursor() as c:
            c.execute("SELECT model FROM train_data;")
            xt = set(c.fetchall())
            y = [i[0] for i in xt]
            encoder = LabelEncoder()
            encoder.fit(y)
            c.execute("CREATE EXTENSION IF NOT EXISTS tsm_system_rows")
            c.execute(
                    sql.SQL("SELECT * FROM {}").format(
                        sql.Identifier("train_metadata")))
            x = np.asarray(c.fetchall())
            q = x[0][1]
            dq = x[0][2]
            diq = x[0][3]
            c.execute(sql.SQL(
                            "SELECT * FROM {} TABLESAMPLE SYSTEM_ROWS(10000)").format(
                            sql.Identifier("eval_data")))
            x = np.asarray(c.fetchall())
            iq_list = x[:, 1]
            y_list = x[:, 2]
            encoded = encoder.transform(y_list)
            yt = np.asarray(to_categorical(encoded, 71))
            q_list = np.asarray([np.transpose([q, iq, dq, diq]) for iq in iq_list])
    # print(sorted(y))

    sql_net("train_data", "train_metadata",
            verbosity=parsed.verbose, save_path=parsed.save_path,
            encoder=encoder, xval=q_list, yval=yt)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main(sys.argv[1:])
