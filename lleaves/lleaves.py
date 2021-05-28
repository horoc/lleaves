from ctypes import CFUNCTYPE, POINTER, c_double, c_int
from pathlib import Path

import llvmlite.binding as llvm
import numpy as np

from lleaves.tree_compiler import ir_from_model_file
from lleaves.tree_compiler.ast import parser
from lleaves.tree_compiler.objective_funcs import get_objective_func


class Model:
    # machine-targeted compiler & exec engine
    _execution_engine = None

    # IR representation of model, as it comes unoptimized from the frontend
    _IR_module_frontend: llvm.ir.Module = None
    # IR Module, optimized by llvmlite
    _IR_module: llvm.ModuleRef = None

    # prediction function
    _c_entry_func = None

    def __init__(self, model_file=None):
        self.model_file = model_file
        self._general_info = parser.parse_model_file(model_file)["general_info"]
        # objective function is implemented as an np.ufunc.
        # TODO move into LLVM instead
        self.objective_transf = get_objective_func(self._general_info["objective"])

    def num_feature(self):
        """number of features"""
        return self._general_info["max_feature_idx"] + 1

    def _get_ir_from_frontend(self):
        if not self._IR_module_frontend:
            self._IR_module_frontend = ir_from_model_file(self.model_file)
        return self._IR_module_frontend

    def _get_execution_engine(self):
        """
        Create an ExecutionEngine suitable for JIT code generation on
        the host CPU. The engine is reusable for an arbitrary number of
        modules.
        """
        if self._execution_engine:
            return self._execution_engine

        llvm.initialize()
        llvm.initialize_native_target()
        llvm.initialize_native_asmprinter()

        # Create a target machine representing the host
        target = llvm.Target.from_default_triple()
        target_machine = target.create_target_machine()
        # And an execution engine with an empty backing module
        backing_mod = llvm.parse_assembly("")
        self._execution_engine = llvm.create_mcjit_compiler(backing_mod, target_machine)
        return self._execution_engine

    def _get_optimized_module(self):
        if self._IR_module:
            return self._IR_module

        # Create a LLVM module object from the IR
        module = llvm.parse_assembly(str(self._get_ir_from_frontend()))
        module.verify()

        # Create optimizer
        pmb = llvm.PassManagerBuilder()
        pmb.opt_level = 3
        pmb.inlining_threshold = 30
        pm_module = llvm.ModulePassManager()
        # Add optimization passes to module-level optimizer
        pmb.populate(pm_module)

        pm_module.run(module)
        self._IR_module = module
        return self._IR_module

    def save_model_ir(self, filepath):
        """
        Save the optimized LLVM IR to filepath.

        This will be optimized specifically to the target machine.
        You should store this together with the model.txt, as certain model features (like the output function)
        are not stored inside the IR.

        :param filepath: file to save to
        """
        Path(filepath).write_text(str(self._get_optimized_module()))

    def load_model_ir(self, filepath):
        """
        Restore saved LLVM IR.
        Instead of compiling & optimizing the model.txt, the loaded model ir will be used, which saves
        compilation time.

        :param filepath: file to load from
        """
        ir = Path(filepath).read_text()
        module = llvm.parse_assembly(ir)
        self._IR_module = module

    def compile(self):
        """
        Generate the LLVM IR for this model and compile it to ASM
        This function can be called multiple time, but will only compile once.
        """
        if self._c_entry_func:
            return

        # add module and make sure it is ready for execution
        exec_engine = self._get_execution_engine()
        exec_engine.add_module(self._get_optimized_module())
        # run codegen
        exec_engine.finalize_object()
        exec_engine.run_static_constructors()

        # construct entry func
        addr = exec_engine.get_function_address("forest_root")
        # CFUNCTYPE params: void return, pointer to data, n_preds, pointer to results arr
        self._c_entry_func = CFUNCTYPE(
            None, POINTER(c_double), c_int, POINTER(c_double)
        )(addr)

    def predict(self, data):
        self.compile()

        data, n_preds = self._to_1d_ndarray(data)
        ptr_data = data.ctypes.data_as(POINTER(c_double))

        preds = np.zeros(n_preds, dtype=np.float64)
        ptr_preds = preds.ctypes.data_as(POINTER(c_double))
        self._c_entry_func(ptr_data, n_preds, ptr_preds)
        return self.objective_transf(preds)

    def _to_1d_ndarray(self, data):
        if isinstance(data, list):
            try:
                data = np.array(data)
            except BaseException:
                raise ValueError("Cannot convert data list to appropriate np array")

        if not isinstance(data, np.ndarray):
            raise ValueError(f"Expecting list or numpy.ndarray, got {type(data)}")
        if len(data.shape) != 2:
            raise ValueError(
                f"Data must be 2 dimensional, is {len(data.shape)} dimensional"
            )
        n_preds = data.shape[0]
        if data.dtype == np.float64:
            # flatten the array to 1D
            data = np.array(data.reshape(data.size), dtype=np.float64, copy=False)
        else:
            data = np.array(data.reshape(data.size), dtype=np.float64)
        return data, n_preds
