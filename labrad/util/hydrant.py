# Copyright (C) 2007  Matthew Neeley
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
labrad.util.hydrant

Generate random LabRAD data for use in testing.
"""

from random import choice, randint, random, gauss
from datetime import datetime, timedelta

from labrad import types as T

def randType(noneOkay=True, nStructs=0):
    choices = [
        T.LRNone,
        T.LRBool,
        T.LRInt,
        T.LRWord,
        T.LRStr,
        T.LRTime,
        lambda: T.LRValue(randUnits()),
        lambda: T.LRComplex(randUnits()),
        lambda: T.LRCluster(*[randType(noneOkay=False, nStructs=nStructs+1)
                              for _ in range(randint(1,5))]),
        lambda: T.LRList(randType(noneOkay=False, nStructs=nStructs+1),
                                  depth=randint(1,3))
    ]
    if not noneOkay:
        choices = choices[1:]
    if nStructs >= 3:
        choices = choices[:-2]
    return choice(choices)()

def randUnits():
    return choice((None, '', 's', 'ms', 'us', 'ns'))

def randValue(t):
    if isinstance(t, T.LRNone): return genNone()
    if isinstance(t, T.LRBool): return genBool()
    if isinstance(t, T.LRInt): return genInt()
    if isinstance(t, T.LRWord): return genWord()
    if isinstance(t, T.LRStr): return genStr()
    if isinstance(t, T.LRTime): return genTime()
    if isinstance(t, T.LRValue): return genValue(t.units)
    if isinstance(t, T.LRComplex): return genComplex(t.units)
    if isinstance(t, T.LRCluster): return genCluster(*t.items)
    if isinstance(t, T.LRList): return genList(t.elem, t.depth)


def genNone(): return None
def genBool(): return choice((True, False))
def genInt(): return int(randint(-2**31, 2**31-1))
def genWord(): return long(randint(0, 2**32-1))
def genStr():
    length = randint(0, 100)
    return ''.join(chr(randint(0, 255)) for _ in range(length))

def genTime():
    diff = timedelta(seconds=randint(-2**20, 2**20),
                     microseconds=randint(-2**20, 2**20))
    return datetime.now() + diff

def genValue(units=None):
    #return gauss(0, 1)
    return T.Value(gauss(0, 1), units)

def genComplex(units=None):
    #return complex(gauss(0, 1), gauss(0, 1))
    return T.Complex(complex(gauss(0, 1), gauss(0, 1)), units)

def genList(elem, depth=1):
    lengths = [randint(1, 2**(5-depth)) for _ in range(depth)]
    def genNDList(ls):
        if len(ls) == 1:
            return [randValue(elem) for _ in range(ls[0])]
        else:
            return [genNDList(ls[1:]) for _ in range(ls[0])]
    return genNDList(lengths)

def genCluster(*items):
    return tuple(randValue(t) for t in items)


def hoseDown(setting, n=1000):
    for _ in range(n):
        try:
            t = randType()
            v = randValue(t)
            print v, repr(t)
            resp = setting(v)
            assert v == resp
        except Exception, e:
            print 'problem:', str(t), repr(t)
            print str(T.flatten(v)[1]), str(T.flatten(resp)[1])
            #raise