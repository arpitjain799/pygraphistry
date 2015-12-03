from __future__ import print_function
from builtins import str

import random
import numpy
import pandas

from . import pygraphistry
from . import util
from . import graph_vector_pb2
from graph_vector_pb2 import VectorGraph


EDGE = graph_vector_pb2.VectorGraph.EDGE
VERTEX = graph_vector_pb2.VectorGraph.VERTEX


def create(edge_df, node_df, sources, dests, nodeid, node_map):
    vg = graph_vector_pb2.VectorGraph()
    vg.version = 1
    vg.type = VectorGraph.DIRECTED
    vg.nvertices = len(node_map)
    vg.nedges = len(edge_df)

    addEdges(vg, sources, dests, node_map)
    edge_types = storeEdgeAttributes(vg, edge_df)
    node_types = storeNodeAttributes(vg, node_df, nodeid, node_map)

    return  {
        'vgraph': vg,
        'types': {
            'node': node_types,
            'edge': edge_types
        },
    }


def addEdges(vg, sources, dests, node_map):
    for s, d in zip(sources.tolist(), dests.tolist()):
        e = vg.edges.add()
        e.src = node_map[s]
        e.dst = node_map[d]


def storeEdgeAttributes(vg, df):
    node_types = {}

    coltypes = df.columns.to_series().groupby(df.dtypes)
    for dtype, cols in coltypes.groups.items():
        for col in cols:
            enc_type = storeValueVector(vg, df, col, dtype, EDGE)
            node_types[col] = enc_type

    return node_types


def storeNodeAttributes(vg, df, nodeid, node_map):
    ordercol = '__order__'
    edge_types = {}

    df[ordercol] = df[nodeid].map(lambda n: node_map[n])
    df.sort(ordercol, inplace=True)
    df.drop(ordercol, axis=1, inplace=True)
    coltypes = df.columns.to_series().groupby(df.dtypes)

    for dtype, cols in coltypes.groups.items():
        for col in cols:
            enc_type = storeValueVector(vg, df, col, dtype, VERTEX)
            edge_types[col] = enc_type

    return edge_types


def storeValueVector(vg, df, col, dtype, target):
    encoders = {
        'object': objectEncoder,
        'bool': numericEncoder,
        'int8': numericEncoder,
        'int16': numericEncoder,
        'int32': numericEncoder,
        'int64': numericEncoder,
        'float16': numericEncoder,
        'float32': numericEncoder,
        'float64': numericEncoder,
        'datetime64[ns]': datetimeEncoder,
        '<M8[D]': datetimeEncoder
    }
    (vec, enc_type) = encoders[dtype.name](vg, df[col], dtype)
    vec.name = col
    vec.target = target
    return enc_type


def objectEncoder(vg, series, dtype):
    series.where(pandas.notnull(series), '', inplace=True)
    vec = vg.string_vectors.add()
    for val in series.map(lambda x: x.decode('utf8')):
        vec.values.append(val)
    return (vec, {'ctype': 'utf8'})


def numericEncoder(vg, series, dtype):
    def getBestRep(series, candidate_types):
        min = series.min()
        max = series.max()
        tinfo = map(lambda t: numpy.iinfo(t), candidate_types)
        return next(i.dtype for i in tinfo if min >= i.min and max <= i.max)

    typemap = {
        'bool': vg.bool_vectors,
        'int8': vg.int32_vectors,
        'int16': vg.int32_vectors,
        'int32': vg.int32_vectors,
        'int64': vg.int64_vectors,
        'float16': vg.float_vectors,
        'float32': vg.float_vectors,
        'float64': vg.double_vectors
    }

    if dtype.name.startswith('int'):
        candidate_types = [numpy.int8, numpy.int16, numpy.int32, numpy.int64]
        rep_type = getBestRep(series, candidate_types)
    else:
        rep_type = dtype

    vec = typemap[rep_type.name].add()
    for val in series:
        vec.values.append(val)
    return (vec, {'ctype': rep_type.name, 'original_type': dtype.name})


def datetimeEncoder(vg, series, dtype):
    vec = vg.int32_vectors.add()
    util.warn('Casting dates to UNIX epoch (resolution of 1 second)')
    series32 = series.astype('int64').map(lambda x: x / 1e9).astype(numpy.int32)
    for val in series32:
        vec.values.append(val.item())
    return (vec, {'ctype': 'datetime32[s]', 'user_type': 'datetime'})

