#!/usr/bin/python3

import struct
import sys
import types
import unittest
import os
import getopt
import re
import subprocess
import shutil
from pathlib import Path
from pathlib import PurePath
from io import StringIO
import contextlib
import importlib
import tempfile

import libfdt
from libfdt import Fdt, FdtSw, FdtException, QUIET_NOTFOUND, QUIET_ALL

@contextlib.contextmanager
def stdoutIO(stdout=None):
    old = sys.stdout
    if stdout is None:
        stdout = StringIO()
        sys.stdout = stdout
        yield stdout
        sys.stdout = old

class Lopper:
    @staticmethod
    # Finds a node by its prefix
    def node_find( fdt, node_prefix ):
        try:
            node = fdt.path_offset( node_prefix )
        except:
            node = 0

        return node

    @staticmethod
    def node_abspath( fdt, nodeid ):
        node_id_list = [nodeid]
        p = fdt.parent_offset(nodeid,QUIET_NOTFOUND)
        while p != 0:
            node_id_list.insert( 0, p )
            p = fdt.parent_offset(p,QUIET_NOTFOUND)

        retname = ""
        for id in node_id_list:
            retname = retname + "/" + fdt.get_name( id )

        return retname

    # This is just looking up if the property exists, it is NOT matching a
    # property value. Consider this finding a "type" of node
    @staticmethod
    def nodes_with_property( fdt_to_search, propname ):
        node_list = []
        node = 0
        depth = 0
        ret_nodes = []
        while depth >= 0:
            node_list.append([depth, fdt_to_search.get_name(node)])

            prop_list = []
            poffset = fdt_to_search.first_property_offset(node, QUIET_NOTFOUND)
            while poffset > 0:
                prop = fdt_to_search.get_property_by_offset(poffset)
                prop_list.append(prop.name)
                poffset = fdt_to_search.next_property_offset(poffset, QUIET_NOTFOUND)

            if propname in prop_list:
                ret_nodes.append(node)

            node, depth = fdt_to_search.next_node(node, depth, (libfdt.BADOFFSET,))

        return ret_nodes

    @staticmethod
    def process_input( sdt_file, input_files, include_paths ):
        sdt = SystemDeviceTree( sdt_file )
        # is the sdt a dts ?
        if re.search( ".dts*", sdt.dts ):
            # TODO: this only really happens after the device tree bits are
            #       concatenated. This is for testing purposes
            #
            # TODO: really, the input_files shouldn't be passed as a parameter here. They
            #       should either be concatenations, or they should be individually
            #       compiled and loaded later as xforms.
            sdt.dtb = Lopper.dt_compile( sdt.dts, input_files, include_paths )

        # Individually compile the input files. At some point these may be
        # concatenated with the main SDT if dtc is doing some of the work, but for
        # now, libfdt is doing the transforms so we compile them separately
        for ifile in input_files:
            if re.search( ".dts*", ifile ):
                xform = Xform( ifile )
                Lopper.dt_compile( xform.dts, "", include_paths )
                # TODO: look for errors!
                xform.dtb = "{0}.{1}".format(ifile, "dtb")
                sdt.xforms.append( xform )

        return sdt

    #
    # WIP
    #  - a more generic way to delete nodes
    #
    #  - node_prefix can be "" and we start at the root
    #  - action can be "delete" "report" "whitelist" "blacklist" ... TBD
    #  - test_op varies based on the action being taken
    #
    @staticmethod
    def filter_node( sdt, node_prefix, action, test_cmd, verbose=0 ):
        fdt = sdt.FDT
        if verbose:
            print( "[NOTE]: filtering nodes root: %s" % node_prefix )

        if not node_prefix:
            node_prefix = "/"

        try:
            node_list = Lopper.get_subnodes( fdt, node_prefix )
        except:
            node_list = []
            if verbose:
                print( "[WARN]: no nodes found that match prefix %s" % node_prefix )

        # make a list of safe functions
        safe_list = ['Lopper.get_prop', 'Lopper.getphandle', 'Lopper.filter_node', 'Lopper.refcount', 'verbose', 'print']

        # this should work, but isn't resolving the local vars, so we have to add them again in the
        # loop below.
        # references: https://stackoverflow.com/questions/701802/how-do-i-execute-a-string-containing-python-code-in-python
        #             http://code.activestate.com/recipes/52217-replace-embedded-python-code-in-a-string-with-the-/
        safe_dict = dict([ (k, locals().get(k, None)) for k in safe_list ])
        safe_dict['len'] = len
        safe_dict['print'] = print
        safe_dict['get_prop'] = Lopper.get_prop
        safe_dict['getphandle'] = Lopper.getphandle
        safe_dict['filter_node'] = Lopper.filter_node
        safe_dict['refcount'] = Lopper.refcount
        safe_dict['fdt'] = fdt
        safe_dict['sdt'] = sdt
        safe_dict['verbose'] = verbose

        if verbose > 1:
            print( "[INFO]: filter: base safe dict: %s" % safe_dict )
            print( "[INFO]: filter: node list: %s" % node_list )

        for n in node_list:
            # build up the device tree node path
            node_name = node_prefix + n
            node = fdt.path_offset(node_name)
            #print( "---------------------------------- node name: %s" % fdt.get_name( node ) )
            prop_list = Lopper.get_property_list( fdt, node_name )
            #print( "---------------------------------- node props name: %s" % prop_list )

            # Add the current node (n) to the list of safe things
            # NOTE: might not be required
            # safe_list.append( 'n' )
            # safe_list.append( 'node_name' )

            # add any needed builtins back in
            safe_dict['n'] = n
            safe_dict['node'] = node
            safe_dict['node_name' ] = node_name

            # search and replace any template options in the cmd. yes, this is
            # only a proof of concept, you'd never do this like this in the end.
            tc = test_cmd
            tc = tc.replace( "%%FDT%%", "fdt" )
            tc = tc.replace( "%%SDT%%", "sdt" )
            tc = tc.replace( "%%NODE%%", "node" )
            tc = tc.replace( "%%NODENAME%%", "node_name" )
            tc = tc.replace( "%%TRUE%%", "print(\"true\")" )
            tc = tc.replace( "%%FALSE%%", "print(\"false\")" )

            if verbose > 2:
                print( "[INFO]: filter node cmd: %s" % tc )

            with stdoutIO() as s:
                try:
                    exec(tc, {"__builtins__" : None }, safe_dict)
                except Exception as e:
                    print("Something wrong with the code: %s" % e)

            if verbose > 2:
                print( "stdout was: %s" % s.getvalue() )
            if "true" in s.getvalue():
                if "delete" in action:
                    if verbose:
                        print( "[INFO]: deleting node %s" % node_name )
                    fdt.del_node( node, True )
            else:
                pass

    @staticmethod
    def dump_node( node_offset ):
        print( "if I was implemented, I'd dump a node, with more info when --verbose was passed" )

    @staticmethod
    def remove_node_if_not_compatible( fdt, node_prefix, compat_string ):
        if verbose:
            print( "[NOTE]: removing incompatible nodes: %s %s" % (node_prefix, compat_string) )

        node_list = []
        node_list = Lopper.get_subnodes( fdt, node_prefix )
        #print( "node list: %s" % node_list )
        for n in node_list:
            # build up the device tree node path
            node_name = node_prefix + n
            node = fdt.path_offset(node_name)
            # print( "node name: %s" % fdt.get_name( node ) )
            prop_list = Lopper.get_property_list( fdt, node_name )
            # print( "prop list: %s" % prop_list )
            if "compatible" in prop_list:
                # print( "This node has a compatible string!!!" )
                prop_value = fdt.getprop( node, 'compatible' )
                # split on null, since if there are multiple strings in the compat, we
                # need them to be separate
                vv = prop_value[:-1].decode('utf-8').split('\x00')
                # print( "prop_value as strings: %s" % vv )
                if not compat_string in vv:
                    if verbose:
                        print( "[INFO]: deleting node %s" % node_name )
                    fdt.del_node( node, True )

    # source: libfdt tests
    @staticmethod
    def get_subnodes(fdt, node_path):
        """Read a list of subnodes from a node

        Args:
        node_path: Full path to node, e.g. '/subnode@1/subsubnode'

        Returns:
        List of subnode names for that node, e.g. ['subsubnode', 'ss1']
        """
        subnode_list = []
        node = fdt.path_offset(node_path)
        offset = fdt.first_subnode(node, QUIET_NOTFOUND)
        while offset > 0:
            name = fdt.get_name(offset)
            subnode_list.append(name)
            offset = fdt.next_subnode(offset, QUIET_NOTFOUND)

        return subnode_list

    # source: libfdt tests
    @staticmethod
    def get_property_list( fdt, node_path ):
        """Read a list of properties from a node

        Args:
        node_path: Full path to node, e.g. '/subnode@1/subsubnode'

        Returns:
        List of property names for that node, e.g. ['compatible', 'reg']
        """
        prop_list = []
        node = fdt.path_offset(node_path)
        poffset = fdt.first_property_offset(node, QUIET_NOTFOUND)
        while poffset > 0:
            prop = fdt.get_property_by_offset(poffset)
            prop_list.append(prop.name)
            poffset = fdt.next_property_offset(poffset, QUIET_NOTFOUND)

        return prop_list

    #
    # reference routine to walk (and gather) a list of all nodes in
    # the tree.
    #
    @staticmethod
    def walk_nodes( FDT ):
        node_list = []
        node = 0
        depth = 0
        while depth >= 0:
            node_list.append([depth, FDT.get_name(node)])
            node, depth = FDT.next_node(node, depth, (libfdt.BADOFFSET,))

        # print( "node list: %s" % node_list )

    @staticmethod
    def dump_dtb( dtb, outfilename="", verbose=0 ):
        dtcargs = (os.environ.get('LOPPER_DTC') or shutil.which("dtc")).split()
        dtcargs += (os.environ.get("STD_DTC_FLAGS") or "").split()
        dtcargs += (os.environ.get("LOPPER_DTC_BFLAGS") or "").split()
        if outfilename:
            dtcargs += ["-o", "{0}".format(outfilename)]
        dtcargs += ["-I", "dtb", "-O", "dts", dtb]

        if verbose:
            print( "[INFO]: dumping dtb: %s" % dtcargs )

        result = subprocess.run(dtcargs, check = False, stderr=subprocess.PIPE )

    # utility command to get a phandle (as a number) from a node
    @staticmethod
    def getphandle( fdt, node_number ):
        prop = fdt.get_phandle( node_number )
        return prop

    # utility command to get a property (as a string) from a node
    # type can be "simple" or "compound". A string is returned for
    # simple, and a list of properties for compound
    @staticmethod
    def get_prop( fdt, node_number, property_name, type="simple" ):
        prop = fdt.getprop( node_number, property_name, QUIET_NOTFOUND )
        if type == "simple":
            val = Lopper.decode_property_value( prop, 0, type )
        else:
            val = Lopper.decode_property_value( prop, 0, type )

        return val

    @staticmethod
    def dt_compile( sdt, i_files, includes ):
        output_dtb = ""

        # TODO: might need to make 'sdt' absolute for the cpp call below
        sdtname = os.path.basename( sdt )
        sdtname_noext = os.path.splitext(sdtname)[0]

        #
        # step 1: preprocess the file with CPP (if available)
        #
        # Note: this is not processing the included files (i_files) at the
        #       moment .. it may have to, or maybe they are for the
        #       transform block below.

        preprocessed_name = "{0}.pp".format(sdtname)

        ppargs = (os.environ.get('LOPPER_CPP') or shutil.which("cpp")).split()
        # Note: might drop the -I include later
        ppargs += "-nostdinc -I include -undef -x assembler-with-cpp ".split()
        ppargs += (os.environ.get('LOPPER_PPFLAGS') or "").split()
        for i in includes:
            ppargs.append("-I{0}".format(i))
        ppargs += ["-o", preprocessed_name, sdt]
        if verbose:
            print( "[INFO]: preprocessing sdt: %s" % ppargs )
        subprocess.run( ppargs, check = True )

        # step 1b: transforms ?

        # step 2: compile the dtb
        #         dtc -O dtb -o test_tree1.dtb test_tree1.dts
        isoverlay = False
        output_dtb = "{0}.{1}".format(sdtname, "dtbo" if isoverlay else "dtb")

        # make sure the dtb is not on disk, since it won't be overwritten by
        # default. TODO: this could only be done on a -f invocation
        if os.path.exists( output_dtb ):
            os.remove ( output_dtb )

        dtcargs = (os.environ.get('LOPPER_DTC') or shutil.which("dtc")).split()
        dtcargs += (os.environ.get( 'LOPPER_DTC_FLAGS') or "").split()
        if isoverlay:
            dtcargs += (os.environ.get("LOPPER_DTC_OFLAGS") or "").split()
        else:
            dtcargs += (os.environ.get("LOPPER_DTC_BFLAGS") or "").split()
        for i in includes:
            dtcargs += ["-i", i]
        dtcargs += ["-o", "{0}".format(output_dtb)]
        dtcargs += ["-I", "dts", "-O", "dtb", "{0}.pp".format(sdt)]
        if verbose:
            print( "[INFO]: compiling dtb: %s" % dtcargs )

        result = subprocess.run(dtcargs, check = False, stderr=subprocess.PIPE )
        if result is not 0:
            # force the dtb, we need to do processing
            dtcargs += [ "-f" ]
            if verbose:
                print( "[INFO]: forcing dtb generation: %s" % dtcargs )

            result = subprocess.run(dtcargs, check = False, stderr=subprocess.PIPE )
            if result.returncode is not 0:
                print( "[ERROR]: unable to (force) compile %s" % dtcargs )
                sys.exit(1)

        # cleanup: remove the .pp file
        os.remove( preprocessed_name )

        return output_dtb

    @staticmethod
    def input_file_type(infile):
        return PurePath(infile).suffix

    @staticmethod
    def encode_byte_array( values ):
        barray = b''
        for i in values:
            barray = barray + i.to_bytes(4,byteorder='big')
        return barray

    @staticmethod
    def refcount( sdt, nodename ):
        return sdt.node_ref( nodename )

    #
    # Parameters:
    #   - Property object from libfdt
    #   - poffset (property offset) [optional]
    #   - type: simple or compound:<format>
    #           <format> is optional, and can be: dec or hex. 'dec' is the default
    @staticmethod
    def decode_property_value( property, poffset, type="simple", verbose=0 ):
        # these could also be nested. Note: this is temporary since the decoding
        # is sometimes wrong. We need to look at libfdt and see how they are
        # stored so they can be unpacked better.
        if re.search( "simple", type ):
            val = ""
            decode_msg = ""
            try:
                val = property.as_uint32()
                decode_msg = "(uint32): {0}".format(val)
            except:
                pass
            if not val and val != 0:
                try:
                    val = property.as_uint64()
                    decode_msg = "(uint64): {0}".format(val)
                except:
                    pass
            if not val and val != 0:
                try:
                    val = property.as_str()
                    decode_msg = "(string): {0}".format(val)
                except:
                    pass
            if not val and val != 0:
                try:
                    val = property[:-1].decode('utf-8').split('\x00')
                    decode_msg = "(multi-string): {0}".format(val)
                except:
                    pass

            if not val and val != 0:
                decode_msg = "** unable to decode value **"
        else:
            decode_msg = ""
            compound = type.split(":")
            format = "dec"
            if len(compound) == 2:
                if re.search( "hex", compound[1] ):
                    format = "hex"

            num_bits = len(property)
            num_nums = num_bits // 4
            start_index = 0
            end_index = 4
            short_int_size = 4
            val = []
            while end_index <= (num_nums * short_int_size):
                short_int = property[start_index:end_index]
                if format == "hex":
                    converted_int = hex(int.from_bytes(short_int,'big',signed=False))
                else:
                    converted_int = int.from_bytes(short_int,'big',signed=False)
                start_index = start_index + short_int_size
                end_index = end_index + short_int_size
                val.append(converted_int)

        if verbose > 3:
            print( "[DEBUG+]: decoding property: \"%s\" (%s) [%s] --> %s" % (property, poffset, property, decode_msg ) )

        return val

##
##
##
##
##
class SystemDeviceTree:
    def __init__(self, sdt_file):
        self.dts = sdt_file
        self.dtb = ""
        self.xforms = []
        self.modules = []
        self.verbose = 0
        self.node_access = {}

    def setup(self):
        if verbose:
            print( "[INFO]: loading dtb and using libfdt to transform tree" )
        self.use_libfdt = True
        self.FDT = libfdt.Fdt(open(self.dtb, mode='rb').read())

    def write( self, outfilename ):
        byte_array = self.FDT.as_bytearray()

        if self.verbose:
            print( "[INFO]: writing output dtb: %s" % outfilename )

        with open(outfilename, 'wb') as w:
            w.write(byte_array)

    # A thin wrapper + consistent logging and error handling around FDT's
    # node delete
    def node_remove( self, target_node_offset ):
        target_node_name = self.FDT.get_name( target_node_offset )

        if self.verbose > 1:
            print( "[NOTE]: deleting node: %s" % target_node_name )

        self.FDT.del_node( target_node_offset, True )

    def apply_domain_spec(self, tgt_domain):
        tgt_node = Lopper.node_find( self.FDT, tgt_domain )
        if tgt_node != 0:
            if self.verbose:
                print( "[INFO]: domain node found: %s for domain %s" % (tgt_node,tgt_domain) )

            # we can hard code this for now, but it needs to be a seperate routine to look
            # up the domain compatibility properties and carry out actions
            domain_compat = Lopper.get_prop( self.FDT, tgt_node, "compatible" )
            if domain_compat:
                if self.modules:
                    for m in self.modules:
                        if m.is_compat( domain_compat ):
                            m.process_domain( tgt_domain, self, self.verbose )
                            return
                else:
                    if self.verbose:
                        print( "[INFO]: no modules available for domain processing .. skipping" )
                        sys.exit(1)
            else:
                print( "[ERROR]: target domain has no compatible string, cannot apply a specification" )

    # we use the name, rather than the offset, since the offset can change if
    # something is deleted from the tree. But we need to use the full path so
    # we can find it later.
    def node_ref_inc( self, node_name ):
        if verbose > 1:
            print( "[INFO]: tracking access to node %s" % node_name )
        if node_name in self.node_access:
            self.node_access[node_name] += 1
        else:
            self.node_access[node_name] = 1

    # get the refcount for a node.
    # node_name is the full path to a node
    def node_ref( self, node_name ):
        if node_name in self.node_access:
            return self.node_access[node_name]
        return -1

    def transform(self):
        if self.verbose:
            print( "[NOTE]: \'%d\' transform input(s) available" % len(self.xforms))

        # was --target passed on the command line ?
        if target_domain:
            # TODO: the application of the spec needs to be in a loaded file
            self.apply_domain_spec(target_domain)

        # iterate over the transforms
        for x in self.xforms:
            xform_fdt = libfdt.Fdt(open(x.dtb, mode='rb').read())
            # Get all the nodes with a xform property
            xform_nodes = Lopper.nodes_with_property( xform_fdt, "compatible" )

            for n in xform_nodes:
                prop = xform_fdt.getprop( n, "compatible" )
                val = Lopper.decode_property_value( prop, 0 )
                node_name = xform_fdt.get_name( n )

                if self.verbose:
                    print( "[INFO]: ------> processing transform: %s" % val )
                if self.verbose > 2:
                    print( "[DEBUG]: prop: %s val: %s" % (prop.name, val ))
                    print( "[DEBUG]: node name: %s" % node_name )

                # TODO: need a better way to search for the possible transform types
                if re.search( ".*,load,module$", val ):
                    if self.verbose:
                        print( "--------------- [INFO]: node %s is a load module transform" % node_name )
                    try:
                        prop = xform_fdt.getprop( n, 'load' ).as_str()
                        module = xform_fdt.getprop( n, 'module' ).as_str()
                    except:
                        prop = ""

                    if prop:
                        if self.verbose:
                            print( "[INFO]: loading module %s" % prop )
                        mod_file = Path( prop )
                        mod_file_wo_ext = mod_file.with_suffix('')
                        try:
                            mod_file_abs = mod_file.resolve()
                        except FileNotFoundError:
                            print( "[ERROR]: module file %s not found" % prop )
                            sys.exit(1)

                        imported_module = __import__(str(mod_file_wo_ext))
                        self.modules.append( imported_module )

                if re.search( ".*,xform,domain$", val ):
                    if self.verbose:
                        print( "[INFO]: node %s is a compatible domain transform" % node_name )
                    try:
                        prop = xform_fdt.getprop( n, 'domain' ).as_str()
                    except:
                        prop = ""

                    if prop:
                        if self.verbose:
                            print( "[INFO]: domain property found: %s" % prop )

                        self.apply_domain_spec(prop)

                if re.search( ".*,xform,modify$", val ):
                    if self.verbose:
                        print( "[INFO]: node %s is a compatible property modify transform" % node_name )
                    try:
                        prop = xform_fdt.getprop( n, 'modify' ).as_str()
                    except:
                        prop = ""

                    if prop:
                        if self.verbose:
                            print( "[INFO]: modify property found: %s" % prop )

                        # format is: "path":"property":"replacement"
                        #    - modify to "nothing", is a remove operation
                        #    - modify with no property is node operation (rename or remove)
                        modify_expr = prop.split(":")
                        if self.verbose:
                            print( "[INFO]: modify path: %s" % modify_expr[0] )
                            print( "        modify prop: %s" % modify_expr[1] )
                            print( "        modify repl: %s" % modify_expr[2] )

                        if modify_expr[1]:
                            # property operation
                            if not modify_expr[2]:
                                if verbose:
                                    print( "[INFO]: property remove operation detected: %s" % modify_expr[1])
                                self.property_remove( modify_expr[0], modify_expr[1], True )
                            else:
                                print( "[INFO]: property modify operation detected ** currently not implemented **" )
                                # TODO: generalize this to a 'modify' self.property_remove( modify_expr[0], modify_expr[1], True )
                        else:
                            # node operation
                            # in case /<name>/ was passed as the new name, we need to drop them
                            # since they aren't valid in set_name()
                            if modify_expr[2]:
                                modify_expr[2] = modify_expr[2].replace( '/', '' )
                                try:
                                    tgt_node = Lopper.node_find( self.FDT, modify_expr[0] )
                                    if tgt_node != 0:
                                        if self.verbose:
                                            print("[INFO]: renaming %s to %s" % (modify_expr[0], modify_expr[2]))
                                        self.FDT.set_name( tgt_node, modify_expr[2] )
                                except:
                                    pass
                            else:
                                if verbose:
                                    print( "[INFO]: node delete: %s" % modify_expr[0] )

                                node_to_remove = Lopper.node_find( self.FDT, modify_expr[0] )
                                if node_to_remove:
                                    self.node_remove( node_to_remove )

    def property_remove( self, node_prefix = "/", propname = "", recursive = True ):
        node = Lopper.node_find( self.FDT, node_prefix )
        node_list = []
        depth = 0
        while depth >= 0:
            prop_list = []
            poffset = self.FDT.first_property_offset(node, QUIET_NOTFOUND)
            while poffset > 0:
                # if we delete the only property of a node, all calls to the FDT
                # will throw an except. So if we get an exception, we set our poffset
                # to zero to escape the loop.
                try:
                    prop = self.FDT.get_property_by_offset(poffset)
                except:
                    poffset = 0
                    continue

                # print( "propname: %s" % prop.name )
                prop_list.append(prop.name)
                poffset = self.FDT.next_property_offset(poffset, QUIET_NOTFOUND)

                if propname in prop_list:
                    # node is an integer offset, propname is a string
                    if self.verbose:
                        print( "[INFO]: removing property %s from %s" % (propname, self.FDT.get_name(node)) )

                    self.FDT.delprop(node, propname)

            if recursive:
                node, depth = self.FDT.next_node(node, depth, (libfdt.BADOFFSET,))
            else:
                depth = -1

    def property_find( self, propname, remove = False ):
        node_list = []
        node = 0
        depth = 0
        while depth >= 0:
            # todo: node_list isn't currently used .. but will be eventually
            node_list.append([depth, self.FDT.get_name(node)])

            prop_list = []
            poffset = self.FDT.first_property_offset(node, QUIET_NOTFOUND)
            while poffset > 0:
                #print( "poffset: %s" % poffset )
                # if we delete the only property of a node, all calls to the FDT
                # will throw an except. So if we get an exception, we set our poffset
                # to zero to escape the loop.
                try:
                    prop = self.FDT.get_property_by_offset(poffset)
                except:
                    poffset = 0
                    continue

                #print( "propname: %s" % prop.name )
                prop_list.append(prop.name)
                poffset = self.FDT.next_property_offset(poffset, QUIET_NOTFOUND)

                if propname in prop_list:
                    # node is an integer offset, propname is a string
                    if self.verbose:
                        print( "[INFO]: removing property %s from %s" % (propname, self.FDT.get_name(node)) )

                    if remove:
                        self.FDT.delprop(node, propname)

            node, depth = self.FDT.next_node(node, depth, (libfdt.BADOFFSET,))

    def inaccessible_nodes( self, propname ):
        node_list = []
        node = 0
        depth = 0
        while depth >= 0:
            prop_list = []
            poffset = self.FDT.first_property_offset( node, QUIET_NOTFOUND )
            while poffset > 0:
                prop = self.FDT.get_property_by_offset( poffset )
                val = Lopper.decode_property_value( prop, poffset )

                if propname == prop.name:
                    if propname == "inaccessible":
                        # - the labels in the nodes are converted to <0x03>
                        # - and there is an associated node with phandle = <0x03>
                        # - so we need to take the phandle, and find the node that has that value

                        tgt_node = self.FDT.node_offset_by_phandle( val )
                        if not tgt_node in node_list:
                            node_list.append(tgt_node)
                            #node_list.append([depth, self.FDT.get_name(node)])

                        if self.verbose:
                            print( "[NOTE]: %s has inaccessible specified for %s" %
                                       (self.FDT.get_name(node), self.FDT.get_name(tgt_node)))

                poffset = self.FDT.next_property_offset(poffset, QUIET_NOTFOUND)

            node, depth = self.FDT.next_node(node, depth, (libfdt.BADOFFSET,))

        if self.verbose:
            if node_list:
                print( "[INFO]: removing inaccessible nodes: %s" % node_list )

                for tgt_node in node_list:
                    # TODO: catch the errors here, since the target node may not have
                    #       had a proper label, so the phandle may not be valid
                    self.node_remove( tgt_node )

class Xform:
    def __init__(self, xform_file):
        self.dts = xform_file
        self.dtb = ""

def usage():
    prog = os.path.basename(sys.argv[0])
    print('Usage: %s [OPTION] <system device tree> [<output file>]...' % prog)
    print('  -v, --verbose       enable verbose/debug processing (specify more than once for more verbosity)')
    print('  -t, --target        indicate the starting domain for processing (i.e. chosen node or domain label)' )
    print('  -d, --dump          dump a dtb as dts source' )
    print('  -i, --input         process supplied input device tree (or yaml) description')
    print('  -o, --output        output file')
    print('  -f, --force         force overwrite output file(s)')
    print('  -h, --help          display this help and exit')
    print('')

##
##
## Thoughts:
##    - could take stdin as a transform tree
##    - add an option to take a sdt and convert it to yaml (aka pretty print)
##    - may need to take -I for the search paths when we run dtc as part of the processing
##
##

def main():
    global inputfiles
    global output
    global output_file
    global sdt
    global sdt_file
    global verbose
    global force
    global dump_dtb
    global target_domain

    verbose = 0
    output = ""
    inputfiles = []
    force = False
    dump_dtb = False
    target_domain = ""
    try:
        opts, args = getopt.getopt(sys.argv[1:], "t:dfvdhi:o:", ["target=", "dump", "force","verbose","help","input=","output="])
    except getopt.GetoptError as err:
        print('%s' % str(err))
        usage()
        sys.exit(2)

    if opts == [] and args == []:
        usage()
        sys.exit(1)

    for o, a in opts:
        if o in ('-v', "--verbose"):
            verbose = verbose + 1
        elif o in ('-d', "--dump"):
            dump_dtb = True
        elif o in ('-f', "--force"):
            force = True
        elif o in ('-h', '--help'):
            usage()
            sys.exit(0)
        elif o in ('-i', '--input'):
            inputfiles.append(a)
        elif o in ('-t', '--target'):
            target_domain = a
        elif o in ('-o', '--output'):
            output = a
        else:
            assert False, "unhandled option"

    # any args should be <system device tree> <output file>
    for idx, item in enumerate(args):
        # validate that the system device tree file exists
        if idx == 0:
            sdt = item
            sdt_file = Path(sdt)
            try:
                my_abs_path = sdt_file.resolve()
            except FileNotFoundError:
                # doesn't exist
                print( "Error: system device tree %s does not exist" % sdt )
                sys.exit(1)

        # the second input is the output file. It can't already exist, unless
        # --force was passed
        if idx == 1:
            if output:
                print( "Error: output was already provided via -o\n")
                usage()
                sys.exit(1)
            else:
                output = item
                output_file = Path(output)
                if output_file.exists():
                    if not force:
                        print( "Error: output file %s exists, and -f was not passed" % output )
                        sys.exit(1)

    # check that the input files (passed via -i) exist
    for i in inputfiles:
        inf = Path(i)
        if not inf.exists():
            print( "Error: input file %s does not exist" % i )
            sys.exit(1)
        Lopper.input_file_type(i)

if __name__ == "__main__":
    # Main processes the command line, and sets some global variables we
    # use below
    main()

    if verbose:
        print( "" )
        print( "SDT summary:")
        print( "   system device tree: %s" % sdt )
        print( "   transforms: %s" % inputfiles )
        print( "   output: %s" % output )
        print ( "" )

    if dump_dtb:
        Lopper.dump_dtb( sdt, verbose )
        os.sys.exit(0)

    device_tree = Lopper.process_input( sdt, inputfiles, "" )

    device_tree.setup()

    device_tree.verbose = verbose

    device_tree.transform()

    #  When "no-access" is specified in a CPU node, everything not listed in "no-
    #  access" is assumed to be accessible from the CPU.
    #  TODO: this needs to be crosssed checked against "access" references
    #        and resolved as a resource split/mapping.
    inaccessible_nodes = device_tree.inaccessible_nodes( "no-access" )

    # switch on the output format. i.e. we may want to write commands/drivers
    # versus dtb .. and the logic to write them out should be loaded from
    # separate implementation files
    if re.search( ".dtb", output ):
        if verbose:
            print( "[INFO]: dtb output format detected, writing %s" % output )
        device_tree.write( output )
    elif re.search( ".cdo", output ):
        print( "[INFO]: would write a CDO if I knew how" )
    elif re.search( ".dts", output ):
        if verbose:
            print( "[INFO]: dts format detected, writing %s" % output )

        # write the device tree to a temporary dtb
        fp = tempfile.NamedTemporaryFile()
        device_tree.write( fp.name )

        # dump the dtb to a dts
        Lopper.dump_dtb( fp.name, output )

        # close the temp file so it is removed
        fp.close()
    else:
        print( "[ERROR]: could not detect output format" )
        sys.exit(1)