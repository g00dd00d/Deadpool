#########################################################################
# deadpool_dfa is a Python 3 library to help faulting implementations   #
# of whiteboxes and convert their outputs into traces compatible with   #
# DFA tools such as JeanGrey or Riscure Inspector.                      #
#                                                                       #
# Copyright (C) 2016                                                    #
# Original author:   Phil Teuwen <phil@teuwen.org>                      #
# Contributors:                                                         #
#                                                                       #
# This program is free software: you can redistribute it and/or modify  #
# it under the terms of the GNU General Public License as published by  #
# the Free Software Foundation, either version 3 of the License, or     #
# any later version.                                                    #
#                                                                       #
# This program is distributed in the hope that it will be useful,       #
# but WITHOUT ANY WARRANTY; without even the implied warranty of        #
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the         #
# GNU General Public License for more details.                          #
#                                                                       #
# You should have received a copy of the GNU General Public License     #
# along with this program.  If not, see <http://www.gnu.org/licenses/>. #
#########################################################################

import os
import sys
import random
import subprocess
import struct
import datetime
from collections import deque
import signal
import time

def processinput(iblock, blocksize):
    """processinput() helper function
   iblock: int representation of one input block
   blocksize: int (8 for DES, 16 for AES)
   returns a list of strings to be used as args for the target
   default processinput(): returns one string containing the block in hex
"""
    # return None if input can't be injected
    return ['%0*x' % (2*blocksize, iblock)]

def processoutput(output, blocksize):
    """processoutput() helper function
   output: string, textual output of the target
   blocksize: int (8 for DES, 16 for AES)
   returns a int, supposed to be the data block outputted by the target
   default processouput(): expects the output to be directly the block in hex
"""
    # DFA is only possible in presence of output so this function is supposed
    # to return an output under normal conditions.
    # If the fault injection leads to a failure without exploitable output, return None
    return int(output, 16)

def try_processoutput(processoutput):
    def foo(output, blocksize):
        try:
            return processoutput(output.decode(), blocksize)
        except:
            return None
    return foo

class Acquisition:
    def __init__(self, targetbin, targetdata, goldendata, dfa,
                iblock=0x74657374746573747465737474657374,
                processinput=processinput,
                processoutput=processoutput,
                verbose=1,
                maxleaf=256*256,
                minleaf=64,
                minleafnail=8,
                addresses=None,
                start_from_left=True,
                depth_first_traversal=False,
                faults=4,
                minfaultspercol=4,
                timeoutfactor=2,
                savetraces_format='default',
                logfile=None,
                tolerate_error=False,
                shell=False,
                debug=False):
        self.debug=debug
        self.verbose=verbose
        self.tolerate_error=tolerate_error
        self.shell=shell
        if self.verbose>1:
            print("Initializing...")
        # Challenge binary
        self.targetbin = targetbin
        # Tables are in same binary or elsewhere? Beware targetdata gets destroyed!
        self.targetdata = targetdata
        # Gold reference, must be different from targetdata
        self.goldendata=open(goldendata, 'rb').read()
        # Check function, to validate corrupted outputs
        self.check = dfa.check
        # Block size in bytes AES:16, DES:8
        self.blocksize=dfa.blocksize
        # Enum from dfa class
        self.FaultStatus=dfa.FaultStatus
        # Ref iblock
        self.iblock=iblock
        # prepares iblock as list of strings based on its int representation
        self.processed_input=processinput(self.iblock, self.blocksize)
        if not self.processed_input:
            self.processed_input=[]
        # from output bytes returns oblock as int
        # If program may crash, make sure processoutput() returns None in such cases
        self.processoutput = try_processoutput(processoutput)
        # Largest (aligned) block to fault
        self.maxleaf=maxleaf
        # Smallest (aligned) block to fault in discovery phase
        self.minleaf=minleaf
        # Smallest (aligned) block to fault in nail-down phase
        self.minleafnail=minleafnail
        # Tables addresses range:
        # None               = full range
        # (0x1000,0x5000)    = target only specified address range
        # '/path/to/logfile' = replays address ranges specified in this log file
        if addresses is None:
            self.tabletree=deque(self.splitrange((0, len(self.goldendata))))
        elif type(addresses) is str:
            self.tabletree=deque()
            with open(addresses, 'r') as reflog:
                for line in reflog:
                    self.tabletree.extend([(int(line[9:19],16),int(line[20:30],16))])
        else:
            self.tabletree=deque(self.splitrange(addresses))
        # Start faults from the left part or the right part of the range?
        self.start_from_left=start_from_left
        # Depth-first traversal or breadth-first traversal?
        self.depth_first_traversal=depth_first_traversal
        # What faults to try once we've a good candidate position?
        # list of values to XOR: [0x01, 0xff, ...], or number of random faults
        self.faults=faults
        # How many faults per column do we want before stopping?
        self.minfaultspercol=minfaultspercol
        # Timestamp
        self.inittimestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        # Traces format: 'default' / 'trs'
        self.savetraces_format = savetraces_format
        # Logfile
        if logfile is None:
            self.logfile=open('%s_%s.log' % (self.targetbin, self.inittimestamp), 'w')
        else:
            self.logfile=open(logfile, 'w')
        def sigint_handler(signal, frame):
            print('\nGot interrupted!')
            self.finish()
            sys.exit(0)
        def sigusr1_handler(signal, frame):
            self.savetraces()
        signal.signal(signal.SIGINT, sigint_handler)
        signal.signal(signal.SIGUSR1, sigusr1_handler)
        self.timeout=10
        # Prepare golden output
        starttime=time.time()
        oblock,status,index=self.doit(self.goldendata)
        # Set timeout = N times normal execution time
        self.timeout=(time.time()-starttime)*timeoutfactor
        if oblock is None or status is not self.FaultStatus.NoFault:
            raise AssertionError('Error, could not obtain golden output, check your setup!')
        # Register ref output
        self.check(oblock, self.verbose)
        self.encpairs=[(self.iblock, oblock)]
        self.decpairs=[(self.iblock, oblock)]
        self.encstatus=[0,0,0,0]
        self.decstatus=[0,0,0,0]
        if self.verbose>1:
            print("Initialized!")
        if self.verbose>0:
            print('Press Ctrl+C to interrupt')
            print('Send SIGUSR1 to dump intermediate results file: $ kill -SIGUSR1 %i' % os.getpid())

    def savetraces(self):
        if len(self.encpairs) <= 1 and len(self.decpairs) <= 1:
            print('No trace to save, sorry')
            return
        if self.savetraces_format=='default':
            return self.savedefault()
        elif self.savetraces_format == 'trs':
            return self.savetrs()
        else:
            print('Error: unknown format: '+ self.savetraces_format)

    def savedefault(self):
        tracefiles=[]
        for goodpairs, mode in [(self.encpairs, "enc"), (self.decpairs, "dec")]:
            if len(goodpairs) > 1:
                tracefile='dfa_%s_%s-%s_%i.txt' % (mode, self.inittimestamp, datetime.datetime.now().strftime('%H%M%S'), len(goodpairs))
                print('Saving %i traces in %s' % (len(goodpairs), tracefile))
                with open(tracefile, 'wb') as f:
                    for (iblock, oblock) in goodpairs:
                        f.write(('%0*X %0*X\n' % (2*self.blocksize, iblock, 2*self.blocksize, oblock)).encode('utf8'))
                tracefiles.append(tracefile)
        return tracefiles

    def savetrs(self):
        tracefiles=[]
        for goodpairs, mode in [(self.encpairs, "enc"), (self.decpairs, "dec")]:
            if len(goodpairs) > 1:
                trsfile='trs_%s_%s-%s_%i.trs' % (mode, self.inittimestamp, datetime.datetime.now().strftime('%H%M%S'), len(goodpairs))
                print('Saving %i traces in %s' % (len(goodpairs), trsfile))
                with open(trsfile, 'wb') as trs:
                    # Nr of traces
                    trs.write(b'\x41\x04' + struct.pack('<I', len(goodpairs)))
                    # Nr of samples
                    trs.write(b'\x42\x04' + struct.pack('<I', 0))
                    # Sample Coding
                    trs.write(b'\x43\x01\x01')
                    # Length of crypto data
                    trs.write(b'\x44\x02' + struct.pack('<H', 2*self.blocksize))
                    # End of header
                    trs.write(b'\x5F\x00')
                    for (iblock, oblock) in goodpairs:
                        # crypto data
                        trs.write(iblock.to_bytes(self.blocksize,'big')+oblock.to_bytes(self.blocksize,'big'))
                tracefiles.append(trsfile)
        return tracefiles

    def doit(self, table):
        open(self.targetdata, 'wb').write(table)
        if self.targetbin==self.targetdata:
            os.chmod(self.targetbin,0o755)
        if self.debug:
            print(' '.join([self.targetbin] + self.processed_input))
        try:
            if self.tolerate_error:
                proc = subprocess.Popen(' '.join([self.targetbin] + self.processed_input) + '; exit 0', stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, executable='/bin/bash')
            elif self.shell:
                proc = subprocess.Popen(' '.join([self.targetbin] + self.processed_input), stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, executable='/bin/bash')
            else:
                proc = subprocess.Popen([self.targetbin] + self.processed_input, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, errs = proc.communicate(timeout=self.timeout)
        except OSError:
            return (None, self.FaultStatus.Crash, None)
        except subprocess.TimeoutExpired:
            proc.kill()
            return (None, self.FaultStatus.Loop, None)
        if self.debug:
            print(output)
        oblock=self.processoutput(output, self.blocksize)
        if self.debug:
            print(oblock)
            sys.exit(0)
        if oblock is None:
            return (None, self.FaultStatus.Crash, None)
        else:
            status, index=self.check(oblock, self.verbose, self.FaultStatus)
        return (oblock, status, index)

    def splitrange(self, r, mincut=1):
        x,y=r
        if y-x <= self.maxleaf and mincut == 0:
            return deque([r])
        # Let's split range into power of two and remaining
        left=1<<(((y-x-1)//2)).bit_length()
        if mincut>0:
            mincut=mincut-1
        dq=self.splitrange((x,x+left), mincut)
        dq.extend(self.splitrange((x+left,y), mincut))
        return dq

    def inject(self, r, fault):
        return self.goldendata[:r[0]]+bytes([x^fault for x in self.goldendata[r[0]:r[1]]])+self.goldendata[r[1]:]

    def dig(self, tree=None, faults=None, level=0, candidates=[]):
        if tree is None:
            tree=self.tabletree
        if faults is None:
            faults=self.faults
        if not self.depth_first_traversal:
            breadth_first_level_address=None
        while len(tree)>0:
            if type(faults) is list:
                fault=faults[0]
            else:
                fault=random.randint(1,255)
            if self.start_from_left:
                r=tree.popleft()
                if not self.depth_first_traversal:
                    if breadth_first_level_address is not None and r[0] < breadth_first_level_address:
                        level+=1
                    breadth_first_level_address = r[0]
            else:
                r=tree.pop()
                if not self.depth_first_traversal:
                    if breadth_first_level_address is not None and r[1] > breadth_first_level_address:
                        level+=1
                    breadth_first_level_address = r[1]
            table=self.inject(r, fault)
            oblock,status,index=self.doit(table)
            log='Lvl %03i [0x%08X-0x%08X[ ^0x%02X %0*X ->' % (level, r[0], r[1], fault, 2*self.blocksize, self.iblock)
            if oblock is not None:
                log+=' %0*X' % (2*self.blocksize, oblock)
            log+=' '+status.name
            if status in [self.FaultStatus.GoodEncFault, self.FaultStatus.GoodDecFault]:
                log+=' Column:'+str(index)
            if self.verbose>1:
                print(log)
            if status in [self.FaultStatus.NoFault, self.FaultStatus.MinorFault]:
                continue
            elif status in [self.FaultStatus.GoodEncFault, self.FaultStatus.GoodDecFault]:
                if r[1]>r[0]+self.minleafnail:
                    # Nailing phase: always depth-first is ok
                    if self.verbose>2:
                        print('Nailing [0x%08X-0x%08X[' % (r[0], r[1]))
                    del(table)
                    if self.dig(self.splitrange(r), faults, level+1):
                        return True
                    continue
                else:
                    mycandidates=candidates+[(log, (self.iblock, oblock))]
                    if type(faults) is list and len(faults)>1:
                        del(table)
                        if self.dig(deque([r]), faults[1:], level, mycandidates):
                            return True
                        continue
                    elif type(faults) is int and faults>1:
                        del(table)
                        if self.dig(deque([r]), faults-1, level, mycandidates):
                            return True
                        continue
                    else:
                        while len(mycandidates)>0:
                            txt,pair = mycandidates.pop(0)
                            if self.verbose>0:
                                print(txt+' Logged')
                            if status is self.FaultStatus.GoodEncFault:
                                self.encpairs.append(pair)
                                self.encstatus[index]+=1
                                if [x for x in self.encstatus if x < self.minfaultspercol] == []:
                                    return True
                            else:
                                self.decpairs.append(pair)
                                self.decstatus[index]+=1
                                if [x for x in self.decstatus if x < self.minfaultspercol] == []:
                                    return True
                            self.logfile.write(txt+'\n')
                        self.logfile.flush()
                        continue
            elif status in [self.FaultStatus.MajorFault, self.FaultStatus.Loop, self.FaultStatus.Crash]:
                if r[1]>r[0]+self.minleaf:
                    if self.depth_first_traversal:
                        del(table)
                        if self.dig(self.splitrange(r), faults, level+1):
                            return True
                        continue
                    else: # breadth-first traversal
                        if self.start_from_left:
                            tree.extend(self.splitrange(r))
                            continue
                        else:
                            tree.extendleft(reversed(self.splitrange(r)))
                            continue
                else:
                    continue
        return False

    def run(self):
        self.dig()
        tracefiles=self.savetraces()
        os.remove(self.targetdata)
        self.logfile.close()
        return tracefiles