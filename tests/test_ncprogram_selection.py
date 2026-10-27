#!/usr/bin/env python3
"""
Test Suite: NCProgram-driven Setup and Operation Selection.

Validates:
1. NCProgram with full setup selection selects all active operations of those setups.
2. NCProgram with a subset of setups excludes unselected setups completely.
3. NCProgram with specific operations within a setup includes only those operations.
4. NCProgram with mixed setups and individual operations correctly filters each setup.
5. NCProgram with folder CAM objects includes only the operations from that folder.
6. Empty NCProgram falls back safely to all setups in cam.setups.
7. Suppressed operations and operations without toolpaths are excluded.
"""

import sys
import types
import unittest

# Ensure adsk mocks exist for headless test execution
if "adsk" not in sys.modules:
    adsk = types.ModuleType("adsk")
    adsk.cam = types.ModuleType("adsk.cam")
    adsk.cam.OperationTypes = types.SimpleNamespace(TurningOperation=1, MillingOperation=0)
    adsk.cam.Setup = type("Setup", (), {})
    adsk.cam.Operation = type("Operation", (), {})
    adsk.cam.CAMFolder = type("CAMFolder", (), {})
    adsk.cam.CAMPattern = type("CAMPattern", (adsk.cam.CAMFolder,), {})
    adsk.core = types.ModuleType("adsk.core")
    adsk.core.CommandCreatedEventHandler = type("CommandCreatedEventHandler", (), {})
    adsk.core.InputChangedEventHandler = type("InputChangedEventHandler", (), {})
    adsk.core.ValidateInputsEventHandler = type("ValidateInputsEventHandler", (), {})
    adsk.core.CommandEventHandler = type("CommandEventHandler", (), {})
    adsk.core.Application = types.SimpleNamespace(get=lambda: types.SimpleNamespace(log=lambda msg: None))
    adsk.fusion = types.ModuleType("adsk.fusion")
    sys.modules["adsk"] = adsk
    sys.modules["adsk.cam"] = adsk.cam
    sys.modules["adsk.core"] = adsk.core
    sys.modules["adsk.fusion"] = adsk.fusion

from PostProcessAll import (
    GetNcProgramSetupsAndOperations,
    GetNcProgram,
)


class MockTool:
    def __init__(self, tool_number=1):
        self.parameters = types.SimpleNamespace(
            itemByName=lambda name: types.SimpleNamespace(
                value=types.SimpleNamespace(value=tool_number)
            )
        )


class MockOperation:
    def __init__(self, name, op_id, tool_number=1, is_suppressed=False, has_toolpath=True, parent=None):
        self.name = name
        self.operationId = op_id
        self.isSuppressed = is_suppressed
        self.hasToolpath = has_toolpath
        self.parent = parent
        self.tool = MockTool(tool_number)
        self.objectType = "adsk::cam::Operation"


class MockFolder:
    def __init__(self, name, operations, parent=None):
        self.name = name
        self.operations = list(operations)
        self.allOperations = list(operations)
        self.folders = []
        self.parent = parent
        self.objectType = "adsk::cam::CAMFolder"
        for op in self.operations:
            op.parent = self


class MockPattern(MockFolder, adsk.cam.CAMPattern):
    def __init__(self, name, operations, parent=None):
        super().__init__(name, operations, parent)
        self.objectType = "adsk::cam::CAMPattern"


class MockSetup:
    __hash__ = None  # Emulate unhashable adsk.cam.Setup from Fusion C++ binding!

    def __init__(self, name, operations=None, is_turning=False, is_suppressed=False):
        self.name = name
        self.isSuppressed = is_suppressed
        self.isSelected = False
        self.operationType = 1 if is_turning else 0
        self.objectType = "adsk::cam::Setup"
        self.allOperations = list(operations or [])
        for op in self.allOperations:
            if getattr(op, "parent", None) is None:
                op.parent = self


class MockNCProgram:
    def __init__(self, name="TestNCProgram", operations=None):
        self.name = name
        self.operations = list(operations or [])


class MockNCProgramsCollection:
    def __init__(self, programs=None):
        self._programs = list(programs or [])

    @property
    def count(self):
        return len(self._programs)

    def item(self, idx):
        return self._programs[idx]

    def __iter__(self):
        return iter(self._programs)

    def __len__(self):
        return len(self._programs)


class MockCAM:
    def __init__(self, setups, nc_programs=None):
        self.setups = list(setups)
        self.ncPrograms = MockNCProgramsCollection(nc_programs)
        all_ops = []
        for s in self.setups:
            all_ops.extend(s.allOperations)
        self.allOperations = types.SimpleNamespace(count=len(all_ops))


class TestNCProgramSelection(unittest.TestCase):
    def setUp(self):
        # Setup 1: 3 operations
        self.op1_1 = MockOperation("Face1", 101, tool_number=1)
        self.op1_2 = MockOperation("Rough1", 102, tool_number=1)
        self.op1_3 = MockOperation("Finish1", 103, tool_number=2)
        self.setup1 = MockSetup("Setup1", [self.op1_1, self.op1_2, self.op1_3])

        # Setup 2: 2 operations
        self.op2_1 = MockOperation("Drill2", 201, tool_number=3)
        self.op2_2 = MockOperation("Tap2", 202, tool_number=4)
        self.setup2 = MockSetup("Setup2", [self.op2_1, self.op2_2])

        # Setup 3: 1 operation
        self.op3_1 = MockOperation("Contour3", 301, tool_number=1)
        self.setup3 = MockSetup("Setup3", [self.op3_1])

        self.cam = MockCAM([self.setup1, self.setup2, self.setup3])
        self.settings = {}

    def test_ncprogram_with_full_setups(self):
        """Selecting full setups in NCProgram returns those setups with all their active operations."""
        prog = MockNCProgram("ProgA", operations=[self.setup1, self.setup2])
        self.cam.ncPrograms = MockNCProgramsCollection([prog])

        target_setups, op_map = GetNcProgramSetupsAndOperations(self.cam, self.settings, program=prog)

        self.assertEqual(len(target_setups), 2)
        self.assertEqual(target_setups[0].name, "Setup1")
        self.assertEqual(target_setups[1].name, "Setup2")

        # Setup 3 must NOT be present
        self.assertNotIn(self.setup3, target_setups)

        # All operations of Setup 1 and Setup 2 must be present
        self.assertEqual(len(op_map[self.setup1]), 3)
        self.assertEqual(len(op_map[self.setup2]), 2)

    def test_ncprogram_filters_to_single_setup(self):
        """NCProgram that references only Setup2 excludes Setup1 and Setup3."""
        prog = MockNCProgram("ProgB", operations=[self.setup2])
        target_setups, op_map = GetNcProgramSetupsAndOperations(self.cam, self.settings, program=prog)

        self.assertEqual(len(target_setups), 1)
        self.assertEqual(target_setups[0].name, "Setup2")
        self.assertEqual(len(op_map[self.setup2]), 2)
        self.assertNotIn(self.setup1, target_setups)
        self.assertNotIn(self.setup3, target_setups)

    def test_ncprogram_filters_to_specific_operations_within_setup(self):
        """NCProgram containing only op1_2 results in only Setup 1 and only op1_2 being processed."""
        prog = MockNCProgram("ProgC", operations=[self.op1_2])
        target_setups, op_map = GetNcProgramSetupsAndOperations(self.cam, self.settings, program=prog)

        self.assertEqual(len(target_setups), 1)
        self.assertEqual(target_setups[0].name, "Setup1")
        self.assertEqual(len(op_map[self.setup1]), 1)
        self.assertEqual(op_map[self.setup1][0].name, "Rough1")
        self.assertEqual(op_map[self.setup1][0].operationId, 102)

    def test_ncprogram_with_mixed_selection(self):
        """NCProgram with Setup 1 (whole) and Op 2_2 (individual) correctly partitions each setup."""
        prog = MockNCProgram("ProgD", operations=[self.setup1, self.op2_2])
        target_setups, op_map = GetNcProgramSetupsAndOperations(self.cam, self.settings, program=prog)

        self.assertEqual(len(target_setups), 2)
        # Setup 1 has all 3 ops
        self.assertEqual(len(op_map[self.setup1]), 3)
        # Setup 2 has only 1 op (Op 2_2)
        self.assertEqual(len(op_map[self.setup2]), 1)
        self.assertEqual(op_map[self.setup2][0].name, "Tap2")
        # Setup 3 is excluded
        self.assertNotIn(self.setup3, target_setups)

    def test_ncprogram_with_folder_operations(self):
        """Operations inside a folder are correctly associated with their setup."""
        folder_op1 = MockOperation("PocketA", 401, tool_number=2)
        folder_op2 = MockOperation("PocketB", 402, tool_number=2)
        free_op = MockOperation("ContourC", 403, tool_number=3)
        folder = MockFolder("PocketsFolder", [folder_op1, folder_op2], parent=self.setup3)
        self.setup3.allOperations = [folder_op1, folder_op2, free_op]
        free_op.parent = self.setup3

        prog = MockNCProgram("ProgFolder", operations=[folder])
        target_setups, op_map = GetNcProgramSetupsAndOperations(self.cam, self.settings, program=prog)

        self.assertEqual(len(target_setups), 1)
        self.assertEqual(target_setups[0].name, "Setup3")
        # Only the folder operations are included; free_op is excluded
        self.assertEqual(len(op_map[self.setup3]), 2)
        op_names = [op.name for op in op_map[self.setup3]]
        self.assertIn("PocketA", op_names)
        self.assertIn("PocketB", op_names)
        self.assertNotIn("ContourC", op_names)

    def test_empty_ncprogram_fallback(self):
        """Empty NCProgram operations collection falls back safely to all setups."""
        prog = MockNCProgram("EmptyProg", operations=[])
        target_setups, op_map = GetNcProgramSetupsAndOperations(self.cam, self.settings, program=prog)

        self.assertEqual(len(target_setups), 3)
        self.assertEqual(len(op_map[self.setup1]), 3)
        self.assertEqual(len(op_map[self.setup2]), 2)
        self.assertEqual(len(op_map[self.setup3]), 1)


    def test_suppressed_and_no_toolpath_filtered_out(self):
        """Suppressed operations and operations with hasToolpath=False are excluded."""
        suppressed_op = MockOperation("SuppressedOp", 501, is_suppressed=True)
        no_toolpath_op = MockOperation("NoPathOp", 502, has_toolpath=False)
        valid_op = MockOperation("ValidOp", 503, has_toolpath=True)
        setup = MockSetup("SpecialSetup", [suppressed_op, no_toolpath_op, valid_op])
        cam = MockCAM([setup])

        prog = MockNCProgram("SpecialProg", operations=[setup])
        target_setups, op_map = GetNcProgramSetupsAndOperations(cam, self.settings, program=prog)

        self.assertEqual(len(target_setups), 1)
        self.assertEqual(len(op_map[setup]), 1)
        self.assertEqual(op_map[setup][0].name, "ValidOp")

    def test_ncprogram_direct_setups_property(self):
        """NCProgram that exposes setups collection directly is supported."""
        prog = MockNCProgram("DirectSetupsProg")
        prog.setups = [self.setup1]
        target_setups, op_map = GetNcProgramSetupsAndOperations(self.cam, self.settings, program=prog)

        self.assertEqual(len(target_setups), 1)
        self.assertEqual(target_setups[0].name, "Setup1")
        self.assertEqual(len(op_map[self.setup1]), 3)

    def test_nested_folders_operation_resolution(self):
        """Operations nested multiple levels deep in CAMFolders resolve to parent setup."""
        deep_op = MockOperation("DeepOp", 601, tool_number=5)
        inner_folder = MockFolder("InnerFolder", [deep_op])
        outer_folder = MockFolder("OuterFolder", [])
        inner_folder.parent = outer_folder
        outer_folder.parent = self.setup1
        self.setup1.allOperations.append(deep_op)

        prog = MockNCProgram("DeepProg", operations=[inner_folder])
        target_setups, op_map = GetNcProgramSetupsAndOperations(self.cam, self.settings, program=prog)

        self.assertEqual(len(target_setups), 1)
        self.assertEqual(target_setups[0].name, "Setup1")
        self.assertEqual(len(op_map[self.setup1]), 1)
        self.assertEqual(op_map[self.setup1][0].name, "DeepOp")

    def test_batch_partitioning_respects_target_operations(self):
        """Verify single-tool batching only includes the specified target_operations."""
        from PostProcessAll import _extract_cam_collection

        # Setup 1 has 3 ops: op1_1 (T1), op1_2 (T1), op1_3 (T2)
        # Pass target_operations containing only op1_2 and op1_3
        target_ops = [self.op1_2, self.op1_3]
        cand_list = _extract_cam_collection(target_ops)
        active_ops = [
            op for op in cand_list
            if not getattr(op, "isSuppressed", False) and getattr(op, "hasToolpath", True)
        ]
        batches = []
        i = 0
        while i < len(active_ops):
            op = active_ops[i]
            curTool = op.tool.parameters.itemByName("tool_number").value.value
            opList = [op]
            i += 1
            while i < len(active_ops):
                next_op = active_ops[i]
                t_num = next_op.tool.parameters.itemByName("tool_number").value.value
                if t_num != curTool:
                    break
                opList.append(next_op)
                i += 1
            batches.append((curTool, opList))

        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0][0], 1)
        self.assertEqual(batches[0][1], [self.op1_2])  # op1_1 was excluded!
        self.assertEqual(batches[1][0], 2)
        self.assertEqual(batches[1][1], [self.op1_3])

    def test_unhashable_setup_as_dict_key(self):
        """Verify that SetupOperationsMap and SetupListDefaultDict never raise TypeError with unhashable Setup."""
        from PostProcessAll import SetupOperationsMap, SetupListDefaultDict, _lookup_setup_ops

        # Verify MockSetup is indeed unhashable
        with self.assertRaises(TypeError):
            hash(self.setup1)

        op_map = SetupOperationsMap()
        # Direct key assignment using unhashable setup object MUST NOT throw TypeError
        op_map[self.setup1] = [self.op1_1]
        self.assertIn(self.setup1, op_map)
        self.assertIn("Setup1", op_map)
        self.assertEqual(op_map[self.setup1], [self.op1_1])
        self.assertEqual(op_map["Setup1"], [self.op1_1])
        self.assertEqual(op_map.get(self.setup1), [self.op1_1])
        self.assertEqual(_lookup_setup_ops(self.setup1, op_map), [self.op1_1])

        default_dict = SetupListDefaultDict()
        default_dict[self.setup1].append(self.op1_2)
        self.assertEqual(default_dict[self.setup1], [self.op1_2])
        self.assertEqual(default_dict["Setup1"], [self.op1_2])

    def test_is_folder_cam_object_recognizes_campattern(self):
        """Verify _is_folder_cam_object recognizes CAMPattern by class, inheritance, and objectType."""
        from PostProcessAll import _is_folder_cam_object

        pattern = MockPattern("CircularPattern1", [self.op1_1], parent=self.setup1)
        self.assertTrue(_is_folder_cam_object(pattern))

    def test_ncprogram_selection_with_campattern_folder(self):
        """Verify that an NCProgram selecting a CAMPattern extracts the operations inside it."""
        from PostProcessAll import GetNcProgramSetupsAndOperations

        pattern = MockPattern("CircularPattern1", [self.op1_1, self.op1_2], parent=self.setup1)
        # Setup has 3 ops, pattern contains 2 ops
        self.setup1.allOperations = [self.op1_1, self.op1_2, self.op1_3]
        program = MockNCProgram(name="PatternProgram", operations=[pattern])

        target_setups, op_map = GetNcProgramSetupsAndOperations(self.cam, self.settings, program=program)
        self.assertEqual(target_setups, [self.setup1])
        selected_ops = op_map.get(self.setup1)
        self.assertEqual(selected_ops, [self.op1_1, self.op1_2])
        self.assertNotIn(self.op1_3, selected_ops)

    def test_check_operation_toolpath_status(self):
        """Verify _check_operation_toolpath_status correctly detects suppressed, empty, and invalid operations."""
        from PostProcessAll import _check_operation_toolpath_status

        # Suppressed op is not flagged
        suppressed_op = MockOperation("SuppressedOp", 701, is_suppressed=True, has_toolpath=False)
        self.assertEqual(_check_operation_toolpath_status(suppressed_op), (False, ""))

        # Op with hasToolpath=False
        empty_op = MockOperation("EmptyOp", 702, has_toolpath=False)
        self.assertEqual(_check_operation_toolpath_status(empty_op), (True, "Empty toolpath"))

        # Op with operationState=3 (NoToolpathOperationState)
        no_tp_op = MockOperation("NoTpOp", 703)
        no_tp_op.operationState = 3
        self.assertEqual(_check_operation_toolpath_status(no_tp_op), (True, "Empty toolpath"))

        # Op with operationState=1 (IsInvalidOperationState)
        invalid_state_op = MockOperation("InvalidStateOp", 704)
        invalid_state_op.operationState = 1
        self.assertEqual(_check_operation_toolpath_status(invalid_state_op), (True, "Invalid toolpath"))

        # Op with isToolpathValid=False
        invalid_validity_op = MockOperation("InvalidValidityOp", 705)
        invalid_validity_op.isToolpathValid = False
        self.assertEqual(_check_operation_toolpath_status(invalid_validity_op), (True, "Invalid toolpath"))

        # Op failing cam.checkToolpath
        mock_cam = types.SimpleNamespace(checkToolpath=lambda op: False)
        failing_check_op = MockOperation("FailingCheckOp", 706)
        self.assertEqual(_check_operation_toolpath_status(failing_check_op, cam=mock_cam), (True, "Invalid toolpath"))

        # Valid op
        valid_op = MockOperation("ValidOp", 707)
        mock_cam_valid = types.SimpleNamespace(checkToolpath=lambda op: True)
        self.assertEqual(_check_operation_toolpath_status(valid_op, cam=mock_cam_valid), (False, ""))

    def test_ncprogram_detects_empty_and_invalid_toolpaths(self):
        """Verify GetNcProgramSetupsAndOperations collects empty/invalid ops and excludes them from valid ops."""
        from PostProcessAll import GetNcProgramSetupsAndOperations

        valid_op = MockOperation("Op1_Valid", 801)
        empty_op = MockOperation("Op2_Empty", 802, has_toolpath=False)
        invalid_op = MockOperation("Op3_Invalid", 803)
        invalid_op.isToolpathValid = False
        suppressed_op = MockOperation("Op4_Suppressed", 804, is_suppressed=True)

        setup = MockSetup("OilLidSetup", operations=[valid_op, empty_op, invalid_op, suppressed_op])
        self.cam.setups = [setup]

        target_setups, op_map = GetNcProgramSetupsAndOperations(self.cam, self.settings, program=MockNCProgram(operations=[]))

        self.assertIn(setup, target_setups)
        # valid_ops must contain only the valid operation
        self.assertEqual(op_map[setup], [valid_op])

        # empty_or_invalid_ops must record empty and invalid operations, excluding suppressed
        empty_invalid = getattr(op_map, "empty_or_invalid_ops", [])
        self.assertEqual(len(empty_invalid), 2)
        flagged_ops = [(s.name, op.name, reason) for s, op, reason in empty_invalid]
        self.assertIn(("OilLidSetup", "Op2_Empty", "Empty toolpath"), flagged_ops)
        self.assertIn(("OilLidSetup", "Op3_Invalid", "Invalid toolpath"), flagged_ops)


if __name__ == "__main__":
    unittest.main()

