#Author-Tim Paterson
#Description-Post process all CAM setups, using the setup name as the output file name.

import adsk.core, adsk.fusion, adsk.cam, sys, traceback, shutil, json, os, os.path, time, re, pathlib, enum, tempfile, subprocess, io, glob, math, datetime, importlib, collections, html, urllib.parse
from typing import Any, Dict, List, Optional, Sequence, Union

# Version number of settings as saved in documents and settings file
# update this whenever settings content changes
version = 16

# Initial default values of settings
defaultSettings = {
    "version" : version,
    "ncProgram": "",
    "output" : "",
    "combineTool" : True,
    "restore_rapid" : True,
    "verifyGcode" : True,
    # Groups are expanded or not
    "groupPersonal" : True
}

# Constants
constCmdName = "Post Process All"
constCmdDefId = "PatersonTech_PostProcessAll"
constCAMWorkspaceId = "CAMEnvironment"
constCAMActionsPanelId = "CAMActionPanel"
constPostProcessControlId = "IronPostProcess"
constCAMProductId = "CAMProductType"
constAttrGroup = constCmdDefId
constAttrName = "settings"
constAttrCompressedName = "CompressedName"
constSettingsFileExt = ".settings"
constPostLoopDelay = 0.1
constLineNumInc = 5
constNcProgramName = "PostProcessAll NC Program"
constAddonDir = os.path.dirname(os.path.abspath(__file__))
constDumperScript = os.path.join(constAddonDir, "universal_dumper.cps")
constReplayerScript = os.path.join(constAddonDir, "universal_replayer.cps")
constPackagesDir = os.path.join(constAddonDir, "packages")

def _dbg_global(msg):
    try:
        t = time.strftime('%H:%M:%S')
        with open("/tmp/postprocess_debug.log", "a", encoding="utf8") as f:
            f.write(f"[{t}] {msg}\n")
            f.flush()
    except Exception:
        pass

# Tool tip text
toolTip = (
    "Post process all setups into G-code for your machine.\n\n"
    "The name of the setup is used for the name of the output "
    "file adding the appropriate extension. A colon (':') in the name indicates "
    "the preceding portion is the name of a subfolder. Multiple "
    "colons can be used to nest subfolders. Spaces around colons "
    "are removed."
    )

# Global list to keep all event handlers in scope.
# This is only needed with Python.
handlers = []

# Global settingsMgr object
settingsMgr = None

def run(context):
    global settingsMgr
    ui = None
    try:
        settingsMgr = SettingsManager()
        app = adsk.core.Application.get()
        ui  = app.userInterface
        InitAddIn()

    except:
        if ui:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


def stop(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface

        # Clean up the UI.
        cmdDef = ui.commandDefinitions.itemById(constCmdDefId)
        if cmdDef:
            cmdDef.deleteMe()
            
        addinsPanel = ui.allToolbarPanels.itemById(constCAMActionsPanelId)
        cmdControl = addinsPanel.controls.itemById(constCmdDefId)
        if cmdControl:
            cmdControl.deleteMe()

        if ui and hasattr(ui, "palettes") and ui.palettes:
            try:
                p = ui.palettes.itemById(constNotificationPaletteId)
                if p:
                    p.deleteMe()
            except Exception:
                pass
    except:
        if ui:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))	


class SettingsManager:
    def __init__(self):
        self.default = None
        self.path = None
        self.fMustSave = False
        self.inputs = None

    def GetSettings(self, docAttr):
        docSettings = None
        attr = docAttr.itemByName(constAttrGroup, constAttrName)
        if attr:
            try:
                docSettings = json.loads(attr.value)
                if docSettings["version"] == version:
                    return docSettings
            except Exception:
                pass
            
        # Document does not have valid settings, get defaults
        if not self.default:
            # Haven't read the settings file yet
            file = None
            try:
                file = open(self.GetPath())
                self.default = json.load(file)
                if self.default["version"] != version:
                    self.UpdateSettings(defaultSettings, self.default)
            except Exception:
                self.default = dict(defaultSettings)
                self.fMustSave = True
            finally:
                if file:
                    file.close
        
        if not docSettings:
            docSettings = dict(self.default)
        else:
            self.UpdateSettings(self.default, docSettings)
        return docSettings

    def SaveDefault(self, docSettings):
        self.fMustSave = False
        self.default = dict(docSettings)
        try:
            strSettings = json.dumps(docSettings)
            file = open(self.GetPath(), "w")
            file.write(strSettings)
            file.close
        except Exception:
            pass

    def SaveSettings(self, docAttr, docSettings):
        if self.fMustSave:
            self.SaveDefault(docSettings)
        docAttr.add(constAttrGroup, constAttrName, json.dumps(docSettings))
            
    def UpdateSettings(self, src, dst):
        if "restore_rapid" not in dst:
            dst["restore_rapid"] = dst.get("fastZ", dst.get("fastZHeuristic", True))
        for k in (
            "splitSetup", "fastZ", "fastZHeuristic", "toolChange", "endCodes", "homeEndsOp",
            "sequence", "twoDigits", "onlySelected", "groupRename", "groupPost", "numericName",
            "groupAdvanced", "initialDelay", "postRetries", "delFiles", "delFolder"
        ):
            dst.pop(k, None)
        for item in src:
            if not (item in dst):
                dst[item] = src[item]
        dst["version"] = src["version"]

    def GetPath(self):
        if not self.path:
            pos = __file__.rfind(".")
            if pos == -1:
                pos = len(__file__)
            self.path = __file__[0:pos] + constSettingsFileExt
        return self.path


def InitAddIn():
    ui = None
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface

        # Create a button command definition.
        cmdDefs = ui.commandDefinitions
        cmdResFolder = os.path.join(constAddonDir, "resources", "Command")
        cmdDef = cmdDefs.addButtonDefinition(constCmdDefId, constCmdName, toolTip, cmdResFolder)
        
        # Connect to the commandCreated event.
        commandEventHandler = CommandEventHandler()
        cmdDef.commandCreated.add(commandEventHandler)
        handlers.append(commandEventHandler)
        
        # Get the Actions panel in the Manufacture workspace.
        workSpace = ui.workspaces.itemById(constCAMWorkspaceId)
        addInsPanel = workSpace.toolbarPanels.itemById(constCAMActionsPanelId)
        
        # Add the button right after the Post Process command.
        cmdControl = addInsPanel.controls.addCommand(cmdDef, constPostProcessControlId, False)
        cmdControl.isPromotedByDefault = True
        cmdControl.isPromoted = True

    except:
        if ui:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


def ExpandFileName(file):
    return os.path.expanduser(file).replace("\\", "/")


def CompressFileName(file):
    # normalize whacks 
    base = os.path.expanduser("~").replace("\\", "/")
    newFile = file.replace("\\", "/").removeprefix(base)
    if len(file) != len(newFile) and newFile[0] == "/":
        file = "~" + newFile
    return file


def AssignOutputFolder(parameters, folder):
    parameters.itemByName("nc_program_output_folder").value.value = folder
    result = parameters.itemByName("nc_program_output_folder").value.value
    if result != folder and folder[0:2] == "\\\\":
        parameters.itemByName("nc_program_output_folder").value.value = "\\\\" + folder    # double up leading "\"
    return None


def _is_setup_cam_object(obj, cam=None):
    if obj is None:
        return False
    if hasattr(adsk.cam, "Setup") and isinstance(obj, adsk.cam.Setup):
        return True
    if getattr(obj, "objectType", "") == "adsk::cam::Setup":
        return True
    if hasattr(obj, "operationType"):
        return True
    if cam and hasattr(cam, "setups"):
        for s in cam.setups:
            if s == obj or (getattr(s, "name", None) is not None and getattr(s, "name", None) == getattr(obj, "name", None)):
                return True
    return False


def _is_folder_cam_object(obj):
    if obj is None:
        return False
    if hasattr(adsk.cam, "CAMFolder") and isinstance(obj, adsk.cam.CAMFolder):
        return True
    if hasattr(adsk.cam, "CAMPattern") and isinstance(obj, adsk.cam.CAMPattern):
        return True
    if getattr(obj, "objectType", "") in ("adsk::cam::CAMFolder", "adsk::cam::CAMPattern"):
        return True
    if hasattr(obj, "folders") and not hasattr(obj, "operationType"):
        return True
    return False


def _extract_cam_collection(collection):
    """Safely extract items from a Fusion CAM collection, list, tuple, or iterable."""
    if collection is None:
        return []
    if isinstance(collection, (list, tuple)):
        return list(collection)
    cnt = getattr(collection, "count", None)
    if isinstance(cnt, int):
        return [collection.item(i) for i in range(cnt)]
    if hasattr(collection, "__iter__"):
        return list(collection)
    return []


def _resolve_setup_for_object(obj, cam=None):
    """Find the owning adsk.cam.Setup for a Setup, Folder, or Operation."""
    if obj is None:
        return None
    if _is_setup_cam_object(obj, cam):
        if cam and hasattr(cam, "setups"):
            for s in cam.setups:
                if s == obj or getattr(s, "name", None) == getattr(obj, "name", None):
                    return s
        return obj

    # Walk up parent chain
    curr = getattr(obj, "parent", None)
    while curr is not None:
        if _is_setup_cam_object(curr, cam):
            if cam and hasattr(cam, "setups"):
                for s in cam.setups:
                    if s == curr or getattr(s, "name", None) == getattr(curr, "name", None):
                        return s
            return curr
        curr = getattr(curr, "parent", None)

    # Search in cam.setups by checking operations
    if cam and hasattr(cam, "setups"):
        for s in cam.setups:
            all_ops = getattr(s, "allOperations", [])
            op_list = _extract_cam_collection(all_ops)
            for op in op_list:
                if op == obj:
                    return s
                if (getattr(op, "operationId", -1) != -1 and 
                    getattr(op, "operationId", -1) == getattr(obj, "operationId", -2)):
                    return s
                if (getattr(op, "name", None) is not None and 
                    getattr(op, "name", None) == getattr(obj, "name", None)):
                    return s
    return None


class SetupOperationsMap(dict):
    """Dictionary mapping Setup to operations list, safe for unhashable adsk.cam.Setup objects."""
    def __setitem__(self, key, value):
        k = getattr(key, "name", key) if not isinstance(key, str) else key
        super().__setitem__(k, value)

    def __getitem__(self, key):
        k = getattr(key, "name", key) if not isinstance(key, str) else key
        return super().__getitem__(k)

    def get(self, key, default=None):
        k = getattr(key, "name", key) if not isinstance(key, str) else key
        return super().get(k, default)

    def __contains__(self, key):
        k = getattr(key, "name", key) if not isinstance(key, str) else key
        return super().__contains__(k)


class SetupListDefaultDict(collections.defaultdict):
    """defaultdict(list) keyed by Setup or setup name, safe for unhashable adsk.cam.Setup objects."""
    def __init__(self):
        super().__init__(list)

    def __setitem__(self, key, value):
        k = getattr(key, "name", key) if not isinstance(key, str) else key
        super().__setitem__(k, value)

    def __getitem__(self, key):
        k = getattr(key, "name", key) if not isinstance(key, str) else key
        return super().__getitem__(k)

    def get(self, key, default=None):
        k = getattr(key, "name", key) if not isinstance(key, str) else key
        return super().get(k, default)

    def __contains__(self, key):
        k = getattr(key, "name", key) if not isinstance(key, str) else key
        return super().__contains__(k)


def _lookup_setup_ops(setup, setup_operations_map):
    """Retrieve operations list for setup from setup_operations_map regardless of wrapper identity."""
    if setup is None or setup_operations_map is None:
        return None
    name = getattr(setup, "name", None)
    if name is not None and name in setup_operations_map:
        return setup_operations_map[name]
    if hasattr(setup_operations_map, "get"):
        return setup_operations_map.get(setup, None)
    return None


def GetNcProgramSetupsAndOperations(cam, settings, setups=None, program=None):
    """
    Look in the selected NCProgram's setup and operations and derive the setups and operations to process.
    Returns:
        target_setups: list of adsk.cam.Setup objects
        setup_operations_map: SetupOperationsMap mapping Setup -> list[Operation]
    """
    if program is None and cam is not None and hasattr(cam, "ncPrograms"):
        program = GetNcProgram(cam, settings)

    whole_setups = []
    selected_ops_by_setup = SetupListDefaultDict()
    nc_prog_setups = []

    def add_setup_ref(s):
        if s and not any(s == existing or getattr(s, "name", None) == getattr(existing, "name", None) for existing in nc_prog_setups):
            nc_prog_setups.append(s)

    # 1. Check if program has direct setups collection
    if program is not None and hasattr(program, "setups"):
        try:
            items = _extract_cam_collection(program.setups)
            for s in items:
                resolved_s = _resolve_setup_for_object(s, cam)
                if resolved_s:
                    add_setup_ref(resolved_s)
                    if not any(resolved_s == ws or getattr(resolved_s, "name", None) == getattr(ws, "name", None) for ws in whole_setups):
                        whole_setups.append(resolved_s)
        except Exception as exc:
            _dbg_global(f"GetNcProgramSetupsAndOperations: error inspecting program.setups: {exc}")

    # 2. Check program.operations collection
    if program is not None and hasattr(program, "operations"):
        try:
            op_items = _extract_cam_collection(program.operations)
            for item in op_items:
                if _is_setup_cam_object(item, cam):
                    resolved_s = _resolve_setup_for_object(item, cam)
                    if resolved_s:
                        add_setup_ref(resolved_s)
                        if not any(resolved_s == ws or getattr(resolved_s, "name", None) == getattr(ws, "name", None) for ws in whole_setups):
                            whole_setups.append(resolved_s)
                elif _is_folder_cam_object(item):
                    resolved_s = _resolve_setup_for_object(item, cam)
                    if resolved_s:
                        add_setup_ref(resolved_s)
                        f_ops = getattr(item, "allOperations", getattr(item, "operations", []))
                        for f_op in _extract_cam_collection(f_ops):
                            selected_ops_by_setup[resolved_s].append(f_op)
                else:
                    # Individual Operation
                    resolved_s = _resolve_setup_for_object(item, cam)
                    if resolved_s:
                        add_setup_ref(resolved_s)
                        selected_ops_by_setup[resolved_s].append(item)
        except Exception as exc:
            _dbg_global(f"GetNcProgramSetupsAndOperations: error inspecting program.operations: {exc}")

    # Fallback if NCProgram specifies no setups/operations
    if not nc_prog_setups:
        if cam and hasattr(cam, "setups"):
            for s in cam.setups:
                add_setup_ref(s)
                if not any(s == ws or getattr(s, "name", None) == getattr(ws, "name", None) for ws in whole_setups):
                    whole_setups.append(s)

    target_setups = list(nc_prog_setups)

    setup_operations_map = SetupOperationsMap()
    for s in target_setups:
        all_s_ops = getattr(s, "allOperations", [])
        all_s_ops_list = _extract_cam_collection(all_s_ops)

        is_whole = any(
            s == ws or getattr(s, "name", None) == getattr(ws, "name", None)
            for ws in whole_setups
        )
        if is_whole:
            valid_ops = [
                op for op in all_s_ops_list
                if not getattr(op, "isSuppressed", False) and getattr(op, "hasToolpath", True)
            ]
        else:
            chosen_ops = selected_ops_by_setup.get(s, [])

            valid_ops = []
            for op in all_s_ops_list:
                is_match = any(
                    op == c_op or
                    (getattr(op, "operationId", -1) != -1 and getattr(op, "operationId", -1) == getattr(c_op, "operationId", -2)) or
                    (getattr(op, "name", None) is not None and getattr(op, "name", None) == getattr(c_op, "name", None))
                    for c_op in chosen_ops
                )
                if is_match and not getattr(op, "isSuppressed", False) and getattr(op, "hasToolpath", True):
                    valid_ops.append(op)

        setup_operations_map[s] = valid_ops

    return target_setups, setup_operations_map


def GetSetups(cam, settings, setups=None, program=None):
    target_setups, _ = GetNcProgramSetupsAndOperations(cam, settings, setups, program)
    return target_setups


def GetNcProgram(cam, settings):
    if not cam or not hasattr(cam, "ncPrograms"):
        return None
    target = None
    prog_name = settings.get("ncProgram") if settings else None
    if prog_name:
        for program in cam.ncPrograms:
            if program.name == prog_name:
                target = program
                break
    if target is None and cam.ncPrograms.count > 0:
        target = cam.ncPrograms.item(0)
    return target


# Event handler for the commandCreated event.
class CommandEventHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            eventArgs = adsk.core.CommandCreatedEventArgs.cast(args)
            cmd = eventArgs.command

            # Get document attributes that will set initial values
            app = adsk.core.Application.get()
            cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType(constCAMProductId))
            docSettings  = settingsMgr.GetSettings(app.activeDocument.attributes)

            # Get the NCProgram
            programs = cam.ncPrograms
            if programs.count == 0:
                ncInput = programs.createInput()
                ncInput.displayName = constNcProgramName
                program = programs.add(ncInput)
                program.postConfiguration = program.postConfiguration
                outputFolder = docSettings["output"]
                program.attributes.add(constAttrGroup, constAttrCompressedName, outputFolder)
                AssignOutputFolder(program.parameters, ExpandFileName(outputFolder))
                program.parameters.itemByName("nc_program_createInBrowser").value.value = True
            elif programs.count == 1:
                program = programs.item(0)
            else:
                haveProgram = False
                for program in programs:
                    if program.name == docSettings["ncProgram"]:
                        haveProgram = True
                        break
                if not haveProgram:
                    program = programs.item(0)              
            docSettings["ncProgram"] = program.name

            # Connect to the execute event.
            onExecute = CommandExecuteHandler(docSettings)
            cmd.execute.add(onExecute)
            handlers.append(onExecute)

            # Add inputs that will appear in a dialog
            inputs = cmd.commandInputs

            # text box as a label for NC Program
            input = inputs.addTextBoxCommandInput("ncProgramLabel", 
                                                   "", 
                                                   "NC Program:",
                                                   1,
                                                   True)
            input.isFullWidth = True
            label = input

            input = inputs.addDropDownCommandInput("ncProgram", 
                                                   "NC Program",
                                                   adsk.core.DropDownStyles.TextListDropDownStyle)
            for listItem in programs:
                input.listItems.add(listItem.name, listItem.name == program.name)
            #input.isFullWidth = True
            input.tooltip = "NC Program to Use"
            input.tooltipDescription = (
                "Post processing will use the settings from the selected NC Program."
            )
            label.tooltip = input.tooltip
            label.tooltipDescription = input.tooltipDescription

            # "Personal Use" version
            inputGroup = inputs.addGroupCommandInput("groupPersonal", "Optimization & Verification")

            # check box to combine operation that use the same tool
            input = inputGroup.children.addBoolValueInput("combineTool",
                                                          "Combine operations using same tool",
                                                          True,
                                                          "",
                                                          docSettings.get("combineTool", True))
            input.tooltip = "Combine Consecutive Operations That Use the Same Tool"
            input.tooltipDescription = (
                "If consecutive operations use the same tool, group them into the same batch. "
                "This optimizes tool changes across all setups.")

            # check box to restore rapid moves
            input = inputGroup.children.addBoolValueInput("restore_rapid",
                                                          "Restore rapid moves (G0)",
                                                          True,
                                                          "",
                                                          docSettings.get("restore_rapid", True))
            input.tooltip = "Restore Safe Rapid Moves (G0)"
            input.tooltipDescription = (
                "Analyze toolpaths and restore safe rapid (G0) moves.<br>"
                "• <b>Turning:</b> Promotes collision-free air moves verified against dynamic 2D IPW simulation.<br>"
                "• <b>Milling:</b> Promotes upward vertical Z+ retract moves reaching or exceeding clearance height.<br>"
                "Restored moves are annotated with comments in the output program.")

            # check box to run 2D collision verification & MRR analysis
            input = inputGroup.children.addBoolValueInput("verifyGcode",
                                                          "Verify G-code & collision guard",
                                                          True,
                                                          "",
                                                          docSettings.get("verifyGcode", True))
            input.tooltip = "Verify G-Code & Collision Guard"
            input.tooltipDescription = (
                "Run toolpath verification, detect potential collisions (Rapid-into-stock, Holder strikes, "
                "Fixture keep-out boundaries), and generate summary report.")
           
            inputGroup.isExpanded = docSettings["groupPersonal"]

            # button to save default settings
            input = inputs.addBoolValueInput("save", "Save as default", False)
            input.resourceFolder = os.path.join(constAddonDir, "resources", "Save")
            input.tooltip = "Save These Settings as System Default"
            input.tooltipDescription = (
                "Save these settings to use as the default for each new design.")

            # text box for error messages
            input = inputs.addTextBoxCommandInput("error", "", "", 3, True)
            input.isFullWidth = True
            input.isVisible = False

            # Connect to the inputChanged event.
            onInputChanged = CommandInputChangedHandler(docSettings)
            cmd.inputChanged.add(onInputChanged)
            handlers.append(onInputChanged)

            # Connect to the validateInputs event.
            onValidateInputs = CommandValidateInputsHandler()
            cmd.validateInputs.add(onValidateInputs)
            handlers.append(onValidateInputs)
        except:
            ui = app.userInterface
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))

# Event handler for the inputChanged event.
class CommandInputChangedHandler(adsk.core.InputChangedEventHandler):
    def __init__(self, docSettings):
        self.docSettings = docSettings
        super().__init__()

    def notify(self, args):
        app = adsk.core.Application.get()
        ui  = app.userInterface
        try:
            eventArgs = adsk.core.InputChangedEventArgs.cast(args)
            cmd = eventArgs.input.parentCommand
            inputs = eventArgs.inputs

            doc = app.activeDocument
            cam = adsk.cam.CAM.cast(doc.products.itemByProductType(constCAMProductId))

            # See if button clicked
            input = eventArgs.input
            if input.id == "save":
                settingsMgr.SaveDefault(self.docSettings)

            elif input.id in self.docSettings:
                if input.objectType == adsk.core.GroupCommandInput.classType():
                    self.docSettings[input.id] = input.isExpanded
                elif input.objectType == adsk.core.DropDownCommandInput.classType():
                    self.docSettings[input.id] = input.selectedItem.name
                else:
                    self.docSettings[input.id] = input.value

        except:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


# Event handler for the validateInputs event.
class CommandValidateInputsHandler(adsk.core.ValidateInputsEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        app = adsk.core.Application.get()
        ui  = app.userInterface

        # No validation currently performed. Skeleton code retained.
        try:
            eventArgs = adsk.core.ValidateInputsEventArgs.cast(args)
            inputs = eventArgs.firingEvent.sender.commandInputs

        except:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


# Event handler for the execute event.
class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, docSettings):
        self.docSettings = docSettings
        super().__init__()

    def notify(self, args):
        eventArgs = adsk.core.CommandEventArgs.cast(args)
        cmd = eventArgs.command
        inputs = cmd.commandInputs

        # Code to react to the event.
        for key in list(self.docSettings.keys()):
            inp = inputs.itemById(key)
            if inp:
                if inp.objectType == adsk.core.GroupCommandInput.classType():
                    self.docSettings[key] = inp.isExpanded
                elif inp.objectType == adsk.core.DropDownCommandInput.classType():
                    if inp.selectedItem:
                        self.docSettings[key] = inp.selectedItem.name
                elif hasattr(inp, "value"):
                    self.docSettings[key] = inp.value
        _dbg_global(f"CommandExecuteHandler execute: combineTool={self.docSettings.get('combineTool')}, verifyGcode={self.docSettings.get('verifyGcode')}, restore_rapid={self.docSettings.get('restore_rapid')}")
        PerformPostProcess(self.docSettings)



def PerformPostProcess(docSettings, setups=None):
    ui = None
    progress = None
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface
        doc = app.activeDocument
        cam = adsk.cam.CAM.cast(doc.products.itemByProductType(constCAMProductId))

        cntFiles = 0
        cntSkipped = 0
        lstSkipped = ""
        verifyResults.clear()
        _dbg_global(f"PerformPostProcess entered: incoming setups count={len(setups) if setups else 0}")

        global settingsMgr
        if settingsMgr is None:
            settingsMgr = SettingsManager()

        program = GetNcProgram(cam, docSettings)
        parameters = program.parameters
        setups, setup_operations_map = GetNcProgramSetupsAndOperations(cam, docSettings, setups, program)

        # normalize output folder for this user
        # "\" is converted to "/"
        outputFolder = parameters.itemByName("nc_program_output_folder").value.value.replace("\\", "/")
        # keep leading "\\" for file share
        if outputFolder[0:2] == "//":
            outputFolder = "\\\\" + outputFolder[2:]
        try:
            pathlib.Path(outputFolder).mkdir(exist_ok=True)
        except Exception as exc:
            # see if we can map it to folder with compressed user
            compressedName = program.attributes.itemByName(constAttrGroup, constAttrCompressedName).value
            if compressedName[0] == "~" and compressedName[1:] == outputFolder[-(len(compressedName) - 1):]:
                # yes, it matches
                outputFolder = ExpandFileName(compressedName)

        compressedName = CompressFileName(outputFolder)
        program.attributes.add(constAttrGroup, constAttrCompressedName, compressedName)
        docSettings["output"] = compressedName

        # Save settings in document attributes
        settingsMgr.SaveSettings(doc.attributes, docSettings)

        if len(setups) != 0 and cam.allOperations.count != 0:
            # Check upfront if any final output file already exists.
            # When the final output file already exists, modally ask user if to overwrite it (Yes/No, No aborts).
            fileExt = parameters.itemByName("nc_program_nc_extension").value.value
            if fileExt and fileExt.lower().lstrip(".") == "json":
                fileExt = ".cnc"
            if not fileExt.startswith("."):
                fileExt = "." + fileExt

            existing_files = []
            for setup in setups:
                target_ops = _lookup_setup_ops(setup, setup_operations_map)
                if not target_ops or getattr(setup, "isSuppressed", False):
                    continue
                if getattr(setup, "allOperations", None) is not None and getattr(setup.allOperations, "count", 1) == 0:
                    continue

                nameList = setup.name.split(':')
                sFolder = outputFolder
                cnt = len(nameList) - 1
                i = 0
                while i < cnt:
                    sFolder += "/" + nameList[i].strip()
                    i += 1
                sFname = nameList[i].strip()
                target_path = os.path.join(sFolder, sFname + fileExt)
                if os.path.isfile(target_path):
                    existing_files.append(target_path)

            if existing_files:
                if ui:
                    if len(existing_files) == 1:
                        target = existing_files[0]
                        prompt = (
                            f"The output file already exists:\n\n"
                            f"{target}\n\n"
                            f"Do you want to overwrite it?"
                        )
                    else:
                        file_lines = "\n".join(f"• {f}" for f in existing_files)
                        prompt = (
                            f"The following output file(s) already exist:\n\n"
                            f"{file_lines}\n\n"
                            f"Do you want to overwrite them?"
                        )
                    res = ui.messageBox(
                        prompt,
                        constCmdName,
                        adsk.core.MessageBoxButtonTypes.YesNoButtonType,
                        adsk.core.MessageBoxIconTypes.QuestionIconType
                    )
                    if res != adsk.core.DialogResults.DialogYes:
                        _dbg_global("PerformPostProcess: user chose not to overwrite existing file; aborting.")
                        return  # abort!

            progress = ui.createProgressDialog() if (ui and hasattr(ui, "createProgressDialog")) else None
            total_setups = max(1, len(setups))
            if progress:
                progress.isCancelButtonShown = True
                progress.show("Post Processing...", "Initializing post processing... (%p%)", 0, 100)
                progress.progressValue = 0
                if "adsk" in sys.modules and hasattr(adsk, "doEvents"):
                    try:
                        adsk.doEvents()
                    except Exception:
                        pass

            def make_setup_progress_cb(s_idx, s_name):
                def _cb(step_msg, frac):
                    if progress:
                        if progress.wasCancelled:
                            return False
                        pct = int(((s_idx + max(0.0, min(1.0, frac))) / total_setups) * 100)
                        progress.message = f"{s_name}: {step_msg} (%p%)"
                        progress.progressValue = max(0, min(100, pct))
                        if "adsk" in sys.modules and hasattr(adsk, "doEvents"):
                            try:
                                adsk.doEvents()
                            except Exception:
                                pass
                    return True
                return _cb

            cntSetups = 0

            # Snapshot original NCProgram parameters so the user's NCProgram is never left mutated
            orig_prog_params = {}
            for p_key in ("nc_program_name", "nc_program_filename", "nc_program_openInEditor"):
                try:
                    p_item = parameters.itemByName(p_key)
                    if p_item is not None:
                        orig_prog_params[p_key] = p_item.value.value
                except Exception:
                    pass

            try:
                for setup in setups:
                    if progress and progress.wasCancelled:
                        break
                    target_ops = _lookup_setup_ops(setup, setup_operations_map)
                    if not target_ops or getattr(setup, "isSuppressed", False):
                        continue
                    if getattr(setup, "allOperations", None) is not None and getattr(setup.allOperations, "count", 1) == 0:
                        continue

                    nameList = setup.name.split(':')    # folder separator
                    setupFolder = outputFolder
                    cnt = len(nameList) - 1
                    i = 0
                    while i < cnt:
                        setupFolder += "/" + nameList[i].strip()
                        i += 1

                    fname = nameList[i].strip()

                    setup_cb = make_setup_progress_cb(cntSetups, setup.name)
                    setup_cb("Starting setup...", 0.0)

                    # post the file with only the operations selected in the NCProgram
                    status = PostProcessSetup(
                        fname, setup, setupFolder, docSettings, program,
                        target_operations=target_ops, progress_cb=setup_cb
                    )
                    if status == None:
                        cntFiles += 1
                        setup_cb("Completed.", 1.0)
                    else:
                        cntSkipped += 1
                        lstSkipped += "\nFailed on setup " + setup.name + ": " + status

                    cntSetups += 1
            finally:
                if progress:
                    try:
                        progress.hide()
                    except Exception:
                        pass
                # restore program output folder and original NCProgram name/filename parameters
                AssignOutputFolder(parameters, outputFolder)
                for p_key, p_val in orig_prog_params.items():
                    try:
                        p_item = parameters.itemByName(p_key)
                        if p_item is not None:
                            p_item.value.value = p_val
                    except Exception:
                        pass

        # done with setups, report results non-modally
        _NotifyPostProcessResult(
            ui=ui,
            cntFiles=cntFiles,
            cntSkipped=cntSkipped,
            lstSkipped=lstSkipped,
            outputFolder=outputFolder,
            verifyResults=verifyResults,
        )
        ShowVerifyResults(ui)

    except:
        if progress:
            progress.hide()
        err_msg = traceback.format_exc()
        _dbg_global("PerformPostProcess exception:\n" + err_msg)
        _NotifyPostProcessResult(
            ui=ui,
            cntFiles=0,
            cntSkipped=1,
            lstSkipped=err_msg,
            outputFolder=outputFolder if 'outputFolder' in locals() else "",
            verifyResults=verifyResults,
            is_fatal=True,
        )

# Per-run list of lathe verification summaries, shown in a modal dialog at the end
verifyResults = []


def _RecordVerifyResult(setup, cncPath, reportText, reportPath=None, pngPath=None, promoted=None, spikes=None, volume_mm3=None, collisions=None, error=None, warning=None):
    basePath = os.path.splitext(cncPath)[0]
    if not reportPath:
        reportPath = basePath + "_report.md"
    if not pngPath:
        pngPath = basePath + ".png"
    try:
        with open(reportPath, "w", encoding="utf8") as f:
            f.write(reportText)
    except Exception:
        pass
    kinds = {}
    for c in (collisions or []):
        kind_str = c.kind.name if hasattr(c.kind, "name") else str(c.kind)
        kinds[kind_str] = kinds.get(kind_str, 0) + 1
    verifyResults.append({
        "setup": setup.name,
        "cnc": cncPath,
        "png": pngPath,
        "report": reportPath,
        "promoted": promoted,
        "spikes": spikes,
        "volume_mm3": volume_mm3,
        "kinds": kinds,
        "error": error,
        "warning": warning,
    })
    try:
        adsk.core.Application.get().log(f"[PostProcessAll] {setup.name}:\n{reportText}")
    except Exception:
        pass


_INTERNAL_HELPER_POSTS = (
    "universal_dumper",
    "universal_replayer",
    "apt_extractor_wrapper",
    "universal_wrapper",
)


def _IsInternalHelperPost(post_cfg) -> bool:
    """Return True if post_cfg refers to one of our internal wrapper/dumper/replayer .cps scripts."""
    if not post_cfg:
        return False
    parts = []
    for attr in ("description", "name", "postPath", "location", "extension"):
        val = getattr(post_cfg, attr, None)
        if val and isinstance(val, str):
            parts.append(val.lower())
    try:
        if hasattr(post_cfg, "url") and post_cfg.url:
            u_str = post_cfg.url.toString() if hasattr(post_cfg.url, "toString") else str(post_cfg.url)
            parts.append(u_str.lower())
    except Exception:
        pass
    joined = " | ".join(parts)
    if any(tag in joined for tag in _INTERNAL_HELPER_POSTS):
        return True
    if "context dumper" in joined or getattr(post_cfg, "extension", "").lower().lstrip(".") == "json":
        return True
    return False


def _GetPostSearchDirs():
    """Return all directories on this machine where Autodesk Fusion and user post processors live."""
    dirs = []
    try:
        from pipeline import find_autodesk_apt_cps, find_autodesk_post_binary
        apt_p = find_autodesk_apt_cps()
        if apt_p:
            d = os.path.dirname(apt_p)
            if os.path.isdir(d) and d not in dirs:
                dirs.append(d)
        post_b = find_autodesk_post_binary()
        if post_b:
            cam_dir = os.path.dirname(post_b)
            for sub in ("Data/Posts", "Posts"):
                c = os.path.join(cam_dir, sub).replace("\\", "/")
                if os.path.isdir(c) and c not in dirs:
                    dirs.append(c)
    except Exception as exc:
        _dbg_global(f"_GetPostSearchDirs pipeline search exception: {exc}")

    std_dirs = [
        constAddonDir,
        os.path.expanduser("~/Library/Application Support/Autodesk/Fusion 360 CAM/Posts"),
        os.path.expanduser("~/Library/Application Support/Autodesk/Autodesk Fusion 360/CAM/Posts"),
        os.path.expanduser("~/Library/Application Support/Autodesk/Autodesk Fusion 360/CAM/cache/posts"),
    ]
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        std_dirs.append(os.path.join(appdata, "Autodesk", "Fusion 360 CAM", "Posts"))
        std_dirs.append(os.path.join(appdata, "Autodesk", "Autodesk Fusion 360", "CAM", "Posts"))
    localappdata = os.environ.get("LOCALAPPDATA", "")
    if localappdata:
        std_dirs.append(os.path.join(localappdata, "Autodesk", "Autodesk Fusion 360", "CAM", "cache", "posts"))

    for d in std_dirs:
        norm_d = d.replace("\\", "/")
        if os.path.isdir(norm_d) and norm_d not in dirs:
            dirs.append(norm_d)

    for pattern in (
        os.path.expanduser("~/Library/Application Support/Autodesk/Autodesk Fusion 360/*/W.login/*/CAMPosts"),
        os.path.join(localappdata, "Autodesk", "Autodesk Fusion 360", "*", "W.login", "*", "CAMPosts") if localappdata else "",
    ):
        if pattern:
            for cd in glob.glob(pattern):
                cd_norm = cd.replace("\\", "/")
                if os.path.isdir(cd_norm) and cd_norm not in dirs:
                    dirs.append(cd_norm)

    return dirs


def _ResolveBasePostPath(program=None, is_turning: Optional[bool] = None) -> str:
    """Resolve target machine post processor path from NCProgram without mutating user settings."""
    search_dirs = _GetPostSearchDirs()

    if program and hasattr(program, "postConfiguration") and program.postConfiguration:
        post_cfg = program.postConfiguration
        if not _IsInternalHelperPost(post_cfg):
            candidates = []
            for attr in ("postPath", "location", "name", "description"):
                val = getattr(post_cfg, attr, None)
                if val and isinstance(val, str):
                    if os.path.isfile(val):
                        # Verify turning/milling compatibility
                        if is_turning is False and "centroid_turning" in os.path.basename(val).lower():
                            pass
                        else:
                            _dbg_global(f"_ResolveBasePostPath: resolved directly via postConfiguration.{attr} -> {val}")
                            return os.path.abspath(val)
                    candidates.append(val)
            try:
                if hasattr(post_cfg, "url") and post_cfg.url:
                    u_str = post_cfg.url.toString() if hasattr(post_cfg.url, "toString") else str(post_cfg.url)
                    if u_str:
                        if u_str.startswith("file://"):
                            import urllib.parse
                            f_path = urllib.parse.unquote(u_str[len("file://"):])
                            if os.path.isfile(f_path):
                                if not (is_turning is False and "centroid_turning" in os.path.basename(f_path).lower()):
                                    _dbg_global(f"_ResolveBasePostPath: resolved via url file:// -> {f_path}")
                                    return os.path.abspath(f_path)
                        candidates.append(os.path.basename(u_str))
            except Exception as u_exc:
                _dbg_global(f"_ResolveBasePostPath url inspection exception: {u_exc}")

            _dbg_global(f"_ResolveBasePostPath: candidate names={candidates}, is_turning={is_turning}")
            for cand in candidates:
                bname = os.path.basename(cand).split("?")[0].strip()
                if not bname:
                    continue
                if is_turning is False and "centroid_turning" in bname.lower():
                    continue
                names_to_try = [bname]
                if not bname.lower().endswith(".cps"):
                    names_to_try.append(bname + ".cps")
                for name_try in names_to_try:
                    for sdir in search_dirs:
                        p_try = os.path.join(sdir, name_try)
                        if os.path.isfile(p_try):
                            _dbg_global(f"_ResolveBasePostPath: matched {name_try} in {sdir} -> {p_try}")
                            return os.path.abspath(p_try)

            # Try matching candidate against post descriptions in search_dirs
            for cand in candidates:
                cand_clean = cand.strip().lower()
                if not cand_clean or len(cand_clean) < 3:
                    continue
                for sdir in search_dirs:
                    if not os.path.isdir(sdir):
                        continue
                    try:
                        for entry in os.listdir(sdir):
                            if entry.lower().endswith(".cps"):
                                entry_path = os.path.join(sdir, entry)
                                if is_turning is False and "centroid_turning" in entry.lower():
                                    continue
                                try:
                                    with open(entry_path, "r", encoding="utf-8", errors="ignore") as f:
                                        first_lines = [f.readline() for _ in range(30)]
                                    for line in first_lines:
                                        if "description" in line and cand_clean in line.lower():
                                            _dbg_global(f"_ResolveBasePostPath: matched description '{cand}' in {entry_path}")
                                            return os.path.abspath(entry_path)
                                except Exception:
                                    pass
                    except Exception:
                        pass

    return ""


def _ExtractPostProperties(program=None) -> dict:
    """Extract custom post processor properties configured on the user's NCProgram."""
    props = {}
    if not program:
        return props
    try:
        post_params = getattr(program, "postParameters", None)
        if not post_params:
            return props
        cnt = getattr(post_params, "count", 0)
        for idx in range(cnt):
            try:
                param = post_params.item(idx)
                p_name = getattr(param, "name", None)
                if not p_name:
                    continue
                val_obj = getattr(param, "value", None)
                val = getattr(val_obj, "value", None) if val_obj is not None else None
                if isinstance(val, (bool, int, float, str)):
                    props[p_name] = val
            except Exception:
                pass
    except Exception as exc:
        _dbg_global(f"_ExtractPostProperties warning: {exc}")
    return props


def _EnsureLocalModulesLoaded():
    """Ensure Add-In directory and packages are on sys.path and reload local modules to avoid stale sys.modules in Fusion 360."""
    import sys
    import importlib
    if constAddonDir not in sys.path:
        sys.path.insert(0, constAddonDir)
    if sys.version_info >= (3, 14) and os.path.isdir(constPackagesDir) and constPackagesDir not in sys.path:
        sys.path.insert(0, constPackagesDir)

    # Only reload geometry.models if its schema is outdated (preserving class identities for isinstance checks)
    if "geometry.models" in sys.modules:
        gm = sys.modules["geometry.models"]
        ltg = getattr(gm, "LatheToolGeometry", None)
        if not ltg or "is_boring" not in getattr(ltg, "__dataclass_fields__", {}):
            try:
                importlib.reload(gm)
            except Exception as exc:
                _dbg_global(f"Reload of geometry.models failed: {exc}")

    local_mod_names = (
        "geometry.engine_interface",
        "geometry.heightfield_engine",
        "geometry.tool_generator",
        "geometry.stock_generator",
        "geometry.legacy_adapter",
        "geometry.lathe_engine",
        "geometry.mill_engine",
        "geometry.plotter",
        "geometry",
        "context_parser",
        "pipeline",
    )
    for mod_name in local_mod_names:
        if mod_name in sys.modules:
            try:
                importlib.reload(sys.modules[mod_name])
            except Exception as exc:
                _dbg_global(f"Reload of {mod_name} failed: {exc}")


def _WriteDumperConfig(base_post_path=None, target_cnc_path=None, post_properties=None):
    """Write sidecar config for universal_dumper.cps when spawned by Fusion 360's C++ host process."""
    cfg = {}
    if base_post_path and os.path.isfile(base_post_path):
        cfg["basePost"] = os.path.abspath(base_post_path)
    if target_cnc_path:
        cfg["targetIntermediateCnc"] = os.path.abspath(target_cnc_path)
    if post_properties and isinstance(post_properties, dict):
        cfg["postProperties"] = post_properties
    for cfg_dir in (tempfile.gettempdir(), "/tmp"):
        try:
            if os.path.isdir(cfg_dir):
                cfg_path = os.path.join(cfg_dir, "universal_dumper_config.json")
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f)
        except Exception:
            pass


def _ClearDumperConfig():
    """Remove temporary sidecar config for universal_dumper.cps."""
    for cfg_dir in (tempfile.gettempdir(), "/tmp"):
        try:
            cfg_path = os.path.join(cfg_dir, "universal_dumper_config.json")
            if os.path.isfile(cfg_path):
                os.remove(cfg_path)
        except Exception:
            pass


def _GetPostConfigObj(post_script_path):
    """Obtain adsk.cam.PostConfiguration object for a .cps script, syncing to Local Post Library if needed."""
    if os.path.isfile(post_script_path):
        local_posts_candidates = [
            os.path.expanduser("~/Library/Application Support/Autodesk/Fusion 360 CAM/Posts"),
        ]
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            local_posts_candidates.append(os.path.join(appdata, "Autodesk", "Fusion 360 CAM", "Posts"))
        for posts_dir in local_posts_candidates:
            try:
                if os.path.isdir(posts_dir):
                    dst_path = os.path.join(posts_dir, os.path.basename(post_script_path))
                    if not os.path.isfile(dst_path) or os.path.getmtime(post_script_path) > os.path.getmtime(dst_path):
                        shutil.copy2(post_script_path, dst_path)
            except Exception as sync_exc:
                _dbg_global(f"Sync of {post_script_path} to {posts_dir} failed: {sync_exc}")

    if hasattr(adsk.cam.PostConfiguration, "createFromContent"):
        try:
            with open(post_script_path, "r", encoding="utf-8") as f:
                return adsk.cam.PostConfiguration.createFromContent(f.read())
        except Exception as exc:
            _dbg_global(f"createFromContent({post_script_path}) failed: {exc}")
    try:
        camMgr = adsk.cam.CAMManager.get()
        if camMgr and camMgr.libraryManager and camMgr.libraryManager.postLibrary:
            postLib = camMgr.libraryManager.postLibrary
            query = postLib.createQuery(adsk.cam.LibraryLocations.LocalLibraryLocation)
            base_name = os.path.basename(post_script_path).lower()
            base_stem = os.path.splitext(base_name)[0]
            for p in query.execute():
                p_name = getattr(p, "name", "").lower()
                p_desc = getattr(p, "description", "").lower()
                p_url = ""
                try:
                    p_url = p.url.toString().lower() if hasattr(p, "url") and hasattr(p.url, "toString") else str(getattr(p, "url", "")).lower()
                except Exception:
                    pass
                if (
                    base_name == p_name
                    or base_stem == p_name
                    or base_name in p_name
                    or base_name in p_url
                    or (base_stem == "universal_dumper" and "universal context & event dumper" in p_desc)
                    or (base_stem == "universal_replayer" and "universal context replayer" in p_desc)
                ):
                    return p
    except Exception as exc2:
        _dbg_global(f"postLibrary query failed: {exc2}")
    return post_script_path


def _ExtractWorkpieceFromSetup(setup):
    """Extract axisymmetric stock and chuck geometry directly from native Fusion 360 Setup."""
    try:
        if not hasattr(setup, "parameters"):
            return None
        params = setup.parameters

        def _get_float_param(names, default=None):
            for n in names:
                try:
                    p = params.itemByName(n)
                    if p is not None:
                        val = p.value.value if hasattr(p, "value") and hasattr(p.value, "value") else p
                        if isinstance(val, (int, float)):
                            if math.isnan(val) or math.isinf(val):
                                continue
                            return float(val) * 10.0  # Fusion internal length unit (cm) to mm
                        elif isinstance(val, str):
                            s = val.strip().lower()
                            if s.endswith("in"):
                                fval = float(s[:-2].strip()) * 25.4
                            elif s.endswith("mm"):
                                fval = float(s[:-2].strip())
                            else:
                                fval = float(s) * 10.0
                            if not math.isnan(fval) and not math.isinf(fval):
                                return fval
                except Exception:
                    pass
            return default

        d_outer_mm = _get_float_param(["job_stockDiameter", "stockDiameter", "job_stockDimensionsX", "job_stockDimensionsY", "stock_diameter"])
        length_mm = _get_float_param(["job_stockLength", "stockLength", "job_stockDimensionsZ", "stock_length"])
        z_max_mm = _get_float_param(["job_stockOffsetFront", "stockOffsetFront", "job_stockOffsetZPositive"], default=0.0)
        if z_max_mm is None or math.isnan(z_max_mm):
            z_max_mm = 0.0

        if d_outer_mm is None or length_mm is None or d_outer_mm <= 0.0 or length_mm <= 0.0:
            # Fallback: inspect stockSolids or models bounding boxes
            try:
                for col_name in ("stockSolids", "models"):
                    col = getattr(setup, col_name, None)
                    if col and hasattr(col, "count") and col.count > 0:
                        for c_idx in range(col.count):
                            item = col.item(c_idx)
                            bb = getattr(item, "boundingBox", None)
                            if bb:
                                z_min_c = bb.minPoint.z * 10.0
                                z_max_c = bb.maxPoint.z * 10.0
                                r_c = max(
                                    math.hypot(bb.minPoint.x, bb.minPoint.y),
                                    math.hypot(bb.maxPoint.x, bb.maxPoint.y),
                                    math.hypot(bb.minPoint.x, bb.maxPoint.y),
                                    math.hypot(bb.maxPoint.x, bb.minPoint.y),
                                ) * 10.0
                                if col_name == "models":
                                    r_c += 1.0  # radial stock margin
                                    z_max_c += 0.5
                                if d_outer_mm is None or (r_c * 2.0 > d_outer_mm):
                                    d_outer_mm = r_c * 2.0
                                if length_mm is None or (z_max_c - z_min_c > length_mm):
                                    length_mm = z_max_c - z_min_c
                                    z_max_mm = z_max_c
                        if d_outer_mm is not None and length_mm is not None:
                            break
            except Exception as b_exc:
                _dbg_global(f"Bounding box stock fallback threw: {b_exc}")

        if d_outer_mm is None or math.isnan(d_outer_mm) or d_outer_mm <= 0.0:
            d_outer_mm = 50.0  # Defensible default
        if length_mm is None or math.isnan(length_mm) or length_mm <= 0.0:
            length_mm = 100.0  # Defensible default

        d_inner_mm = _get_float_param(["job_stockDiameterInner", "stockDiameterInner"], default=0.0)
        if d_inner_mm is None or math.isnan(d_inner_mm) or d_inner_mm < 0.0:
            d_inner_mm = 0.0
        z_min_mm = z_max_mm - length_mm


        fixtures = []
        clamp_len_mm = _get_float_param(["job_stockClampingLength", "stockClampingLength"], default=0.0)
        if clamp_len_mm and clamp_len_mm > 0.1:
            from geometry.models import AxisymmetricChuckObstacle
            fixtures.append(
                AxisymmetricChuckObstacle(
                    z_min=z_min_mm - 50.0,
                    z_max=z_min_mm + clamp_len_mm,
                    outer_radius=(d_outer_mm * 0.5) + 30.0,
                    clamping_radius=d_outer_mm * 0.5,
                )
            )

        from geometry.models import AxisymmetricStock2D, WorkpieceDefinition
        return WorkpieceDefinition(
            stock=AxisymmetricStock2D(
                z_min=z_min_mm,
                z_max=z_max_mm,
                outer_radius=d_outer_mm * 0.5,
                inner_radius=d_inner_mm * 0.5,
            ),
            fixtures=fixtures,
        )
    except Exception as exc:
        _dbg_global(f"_ExtractWorkpieceFromSetup exception: {exc}")
        return None




def _ExtractIntermediateWithDumper(setup, target_cnc_path, base_post_path=None, target_operations=None):
    """
    Extract Compact-NCI intermediate file using universal_dumper.cps
    by querying PostLibrary and posting all valid operations of the setup.
    """
    try:
        dumper_post_obj = _GetPostConfigObj(constDumperScript)
        if not dumper_post_obj:
            _dbg_global("Could not obtain dumper_post_obj")
            return None

        if base_post_path and os.path.isfile(base_post_path):
            os.environ["BASE_POST"] = os.path.abspath(base_post_path)
        os.environ["TARGET_INTERMEDIATE_CNC"] = target_cnc_path
        _WriteDumperConfig(base_post_path=base_post_path, target_cnc_path=target_cnc_path)

        app = adsk.core.Application.get()
        doc = app.activeDocument
        cam = adsk.cam.CAM.cast(doc.products.itemByProductType(constCAMProductId))
        if not cam or not hasattr(cam, "ncPrograms"):
            return None

        tmp_out_dir = tempfile.gettempdir().replace("\\", "/")
        nc_input = cam.ncPrograms.createInput()
        nc_prog = cam.ncPrograms.add(nc_input)
        try:
            nc_prog.postConfiguration = dumper_post_obj
            ops_source = target_operations if target_operations is not None else getattr(setup, "allOperations", [])
            ops_list = _extract_cam_collection(ops_source)
            valid_ops = [op for op in ops_list if not getattr(op, "isSuppressed", False) and getattr(op, "hasToolpath", True)]
            nc_prog.operations = valid_ops if valid_ops else [setup]
            params = nc_prog.parameters
            AssignOutputFolder(params, tmp_out_dir)
            params.itemByName("nc_program_filename").value.value = "dumper_extract_tmp"
            params.itemByName("nc_program_openInEditor").value.value = False
            opts = adsk.cam.NCProgramPostProcessOptions.create()
            if hasattr(opts, "isFailOnToolNumberDuplication"):
                opts.isFailOnToolNumberDuplication = False
            nc_prog.postProcess(opts)
        finally:
            _ClearDumperConfig()
            try:
                nc_prog.deleteMe()
            except Exception:
                pass

        if not os.path.isfile(target_cnc_path):
            fallback_cnc = os.path.join(tmp_out_dir, "dumper_extract_tmp.json.intermediate.cnc")
            if os.path.isfile(fallback_cnc):
                shutil.copy2(fallback_cnc, target_cnc_path)

        if os.path.isfile(target_cnc_path):
            _dbg_global(f"_ExtractIntermediateWithDumper successfully created {target_cnc_path}")
            return target_cnc_path
    except Exception as exc:
        _dbg_global(f"_ExtractIntermediateWithDumper threw: {exc}")
    return None


def RunLatheVerifier(cncPath, setup, docSettings, base_post=None):
    """
    Execute 6-stage toolpath verification and rapid-promotion pipeline:
    1. Ingest WorkpieceDefinition and ToolAssembly models analytically from Setup.
    2. Extract full post context (.json) and Compact-NCI (.cnc) via universal_dumper.cps.
    3. Simulate 2D IPW stock removal and verify collisions via Lathe2DIPWVerifier.
    4. If restore_rapid is enabled, restore native G0 rapid moves via universal_replayer.cps.
    5. Compile markdown verification report and generate high-res verification plot (.png).
    """
    _dbg = _dbg_global
    _dbg(f"RunLatheVerifier entered: cncPath={cncPath}, setup={getattr(setup, 'name', None)}")
    if setup is None or setup.operationType != adsk.cam.OperationTypes.TurningOperation:
        _dbg("RunLatheVerifier: setup is None or not TurningOperation, skipping")
        return None
    verify_flag = docSettings.get("verifyGcode", True)
    fastz_flag = docSettings.get("restore_rapid", True)
    _dbg(f"RunLatheVerifier: verifyGcode={verify_flag}, restore_rapid={fastz_flag}")
    if not (verify_flag or fastz_flag):
        _dbg("RunLatheVerifier: neither verifyGcode nor restore_rapid enabled, skipping")
        return None
    if not os.path.isfile(cncPath):
        _dbg(f"RunLatheVerifier: cncPath does not exist: {cncPath}")
        return None

    try:
        with open(cncPath, "r", encoding="utf8", errors="replace") as f:
            gcode_content = f.read()

        promote = bool(docSettings.get("restore_rapid", True))
        basePath = os.path.splitext(cncPath)[0]
        base_no_ext = basePath
        reportPath = basePath + "_report.md"
        plotPath = basePath + ".png"

        # Ensure add-on root and packages are in sys.path and reload local modules
        _EnsureLocalModulesLoaded()

        can_import_in_process = False
        try:
            import shapely
            from geometry import (
                Lathe2DIPWVerifier,
                ToolAssembly,
                CollisionKind,
                AxisymmetricStock2D,
                WorkpieceDefinition,
            )
            from pipeline import PipelineOrchestrator
            can_import_in_process = True
        except (ImportError, OSError):
            can_import_in_process = False

        # Ingest workpiece directly from native Fusion Setup API
        workpiece = _ExtractWorkpieceFromSetup(setup)
        tools_by_op = {}

        if workpiece and workpiece.stock:
            if math.isnan(workpiece.stock.outer_radius) or workpiece.stock.outer_radius <= 0.0:
                _dbg("RunLatheVerifier: extracted workpiece has invalid/NaN outer_radius, discarding for fallback")
                workpiece = None

        is_inch = ("G20" in gcode_content) or not ("G21" in gcode_content)
        tool_scale = 1.0

        if not workpiece or not tools_by_op:
            from geometry import parse_legacy_stock, parse_legacy_tool_op
            for line in gcode_content.splitlines():
                if not workpiece and line.startswith(";META_STOCK:"):
                    workpiece = parse_legacy_stock(line)
                    if is_inch and workpiece.stock and isinstance(workpiece.stock, AxisymmetricStock2D):
                        workpiece = WorkpieceDefinition(
                            stock=AxisymmetricStock2D(
                                z_min=workpiece.stock.z_min * 25.4,
                                z_max=workpiece.stock.z_max * 25.4,
                                outer_radius=workpiece.stock.outer_radius * 25.4,
                                inner_radius=workpiece.stock.inner_radius * 25.4,
                            ),
                            fixtures=workpiece.fixtures,
                        )
                elif not tools_by_op and line.startswith(";META_OP:"):
                    try:
                        payload = json.loads(line.split(":", 1)[1])
                        geom = parse_legacy_tool_op(line) if can_import_in_process else None
                        tool_asm = ToolAssembly(
                            tool_id=payload.get("tool", 0),
                            name=payload.get("name", ""),
                            geometry=geom,
                        ) if can_import_in_process else None
                        if tool_asm and tool_asm.name:
                            tools_by_op[tool_asm.name] = tool_asm
                    except Exception:
                        pass
            if is_inch:
                tool_scale = 25.4

        _dbg(f"RunLatheVerifier: workpiece={workpiece}, tools count={len(tools_by_op)}")
        if not workpiece:
            _dbg(f"RunLatheVerifier: workpiece extraction returned None! Skipping {cncPath}")
            return None

        # Base post processor configuration
        if not base_post or not isinstance(base_post, str) or not os.path.isfile(base_post):
            base_post = os.path.join(constAddonDir, "centroid_turning.cps")
        if not os.path.isfile(base_post):
            return f"Base machine post processor not found: {base_post}"

        # Locate intermediate Compact-NCI (.cnc)
        intermediate_path = cncPath + ".intermediate.cnc"
        if not os.path.isfile(intermediate_path):
            cand_list = [
                base_no_ext + ".intermediate.cnc",
                os.path.join(tempfile.gettempdir(), os.path.basename(base_no_ext) + ".intermediate.cnc"),
            ]
            for cand in cand_list:
                if os.path.isfile(cand):
                    intermediate_path = cand
                    break

        if not os.path.isfile(intermediate_path) and setup:
            extracted = _ExtractIntermediateWithDumper(setup, base_no_ext + ".intermediate.cnc", base_post_path=base_post)
            if extracted and os.path.isfile(extracted):
                intermediate_path = extracted

        _dbg(f"RunLatheVerifier: intermediate_path={intermediate_path}, exists={os.path.isfile(intermediate_path)}")
        if not os.path.isfile(intermediate_path):
            _dbg(f"RunLatheVerifier: Intermediate toolpath not found for {cncPath}! Skipping verification.")
            return None

        if can_import_in_process:
            verifier = Lathe2DIPWVerifier(tool_scale_to_mm=tool_scale)
            orchestrator = PipelineOrchestrator(
                dumper_cps=constDumperScript,
                replayer_cps=constReplayerScript,
            )

            # Run 6-stage end-to-end pipeline in-process
            report, final_gcode = orchestrator.run(
                intermediate_cnc_path=intermediate_path,
                base_post_path=base_post,
                workpiece=workpiece,
                tools_by_op=tools_by_op,
                verifier=verifier,
                final_cnc_path=cncPath,
                report_output_path=reportPath,
                plot_output_path=plotPath,
                promote=promote,
            )

            fatal_cols = [
                c for c in report.collisions
                if c.kind in (CollisionKind.RAPID_INTO_STOCK, CollisionKind.HOLDER_STRIKE, CollisionKind.FIXTURE_COLLISION)
            ]
            warn = f"{len(fatal_cols)} collision(s) detected!" if fatal_cols else None
            promoted_cnt = len(report.promoted_vectors) if promote else 0
            spikes_cnt = len(report.mrr_spikes)
            vol_mm3 = report.total_volume_removed_mm3
            cols_list = report.collisions
        else:
            # Subprocess execution via isolated venv Python (bypasses macOS hardened runtime Team ID restriction)
            venvCandidates = [
                os.path.join(constAddonDir, ".venv", "bin", "python"),
                os.path.join(constAddonDir, ".venv", "bin", "python3"),
                os.path.expanduser("~/Documents/.lathe_venv/bin/python"),
            ]
            venvPy = next((p for p in venvCandidates if os.path.isfile(p)), None)
            if not venvPy:
                return "Lathe verifier dependencies (shapely) could not be loaded in Fusion process or isolated venv."

            runner = os.path.join(constAddonDir, "run_pipeline.py")
            tools_tmp_path = None
            try:
                cmd = [
                    venvPy, runner,
                    "--intermediate", intermediate_path,
                    "--base-post", base_post,
                    "--final-cnc", cncPath,
                    "--report", reportPath,
                    "--plot", plotPath,
                ]
                if tools_data:
                    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf8") as tf:
                        json.dump(tools_data, tf)
                        tools_tmp_path = tf.name
                    cmd.extend(["--tools-file", tools_tmp_path])
                else:
                    cmd.extend(["--meta-gcode", cncPath])

                if not promote:
                    cmd.append("--no-promote")
                if workpiece and hasattr(workpiece, "stock") and workpiece.stock:
                    stock_json = json.dumps({
                        "z_min": workpiece.stock.z_min,
                        "z_max": workpiece.stock.z_max,
                        "outer_radius": workpiece.stock.outer_radius,
                        "inner_radius": workpiece.stock.inner_radius,
                    })
                    cmd.extend(["--stock-json", stock_json])
                if workpiece and hasattr(workpiece, "fixtures") and workpiece.fixtures:
                    fixtures_json = json.dumps([
                        {
                            "z_min": f.z_min,
                            "z_max": f.z_max,
                            "outer_radius": f.outer_radius,
                            "clamping_radius": f.clamping_radius,
                        }
                        for f in workpiece.fixtures
                    ])
                    cmd.extend(["--fixtures-json", fixtures_json])

                cleanEnv = {
                    k: v for k, v in os.environ.items()
                    if not k.startswith("PYTHON") and k not in ("DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH")
                }
                _dbg(f"RunLatheVerifier: running subprocess: {cmd}")
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=cleanEnv)
                _dbg(f"RunLatheVerifier: subprocess completed, exit={proc.returncode}, stdout_len={len(proc.stdout)}, stderr_len={len(proc.stderr)}")
                if proc.returncode != 0:
                    err_line = (proc.stderr.strip().splitlines() or proc.stdout.strip().splitlines() or [f"exit code {proc.returncode}"])[-1]
                    _dbg(f"RunLatheVerifier: subprocess failed: {err_line}")
                    return f"Verification pipeline failed: {err_line}"

                vol_mm3 = 0.0
                promoted_cnt = 0
                spikes_cnt = 0
                collision_cnt = 0
                for line in proc.stdout.splitlines():
                    if line.startswith("VERIFY_RESULT:"):
                        res_data = json.loads(line.split(":", 1)[1])
                        vol_mm3 = res_data.get("volume_mm3", 0.0)
                        promoted_cnt = res_data.get("promoted", 0)
                        spikes_cnt = res_data.get("spikes", 0)
                        collision_cnt = res_data.get("collisions", 0)

                cols_list = []
                warn = f"{collision_cnt} collision(s) detected!" if collision_cnt > 0 else None
            finally:
                if tools_tmp_path and os.path.isfile(tools_tmp_path):
                    try:
                        os.remove(tools_tmp_path)
                    except Exception:
                        pass

        # Cleanup sidecar intermediate file
        sidecar = cncPath + ".intermediate.cnc"
        if os.path.isfile(sidecar):
            try:
                os.remove(sidecar)
            except Exception:
                pass

        report_md = ""
        if os.path.isfile(reportPath):
            with open(reportPath, "r", encoding="utf8", errors="replace") as rf:
                report_md = rf.read()

        _dbg(f"RunLatheVerifier: recording result for {setup.name}: cols={len(cols_list)}, promoted={promoted_cnt}, warn={warn}")
        _RecordVerifyResult(
            setup=setup,
            cncPath=cncPath,
            reportText=report_md,
            reportPath=reportPath,
            pngPath=plotPath,
            promoted=promoted_cnt,
            spikes=spikes_cnt,
            volume_mm3=vol_mm3,
            collisions=cols_list,
            error=None,
            warning=warn,
        )

        if warn:
            _dbg(f"RunLatheVerifier: WARNING: {warn} in {os.path.basename(cncPath)}. Final NC code generated.")
            try:
                adsk.core.Application.get().log(f"[PostProcessAll] WARNING: {warn} in setup {setup.name}")
            except Exception:
                pass

        return None

    except Exception as exc:
        _dbg(f"RunLatheVerifier threw exception: {exc}\n{traceback.format_exc()}")
        return f"Lathe pipeline verifier failed on {os.path.basename(cncPath)}: {exc}"


_palette_handlers = []
_last_action_timestamp = 0
_last_action_key = ""
constNotificationPaletteId = "PostProcessAll_NotificationPalette"

_HTMLEventHandlerBase = (
    getattr(adsk.core, "HTMLEventHandler", object)
    if ("adsk" in sys.modules and hasattr(adsk, "core") and hasattr(adsk.core, "HTMLEventHandler"))
    else object
)

_NavigationEventHandlerBase = (
    getattr(adsk.core, "NavigationEventHandler", object)
    if ("adsk" in sys.modules and hasattr(adsk, "core") and hasattr(adsk.core, "NavigationEventHandler"))
    else object
)


def _HandleNotificationAction(palette_id, action, data):
    global _last_action_timestamp, _last_action_key
    now = time.time()
    key = f"{action}:{data}"
    # Debounce duplicate events if both Navigation and HTML handlers fire for the same click
    if key == _last_action_key and (now - _last_action_timestamp) < 0.8:
        _dbg_global(f"_HandleNotificationAction: debouncing duplicate action={action}")
        return
    _last_action_key = key
    _last_action_timestamp = now

    _dbg_global(f"_HandleNotificationAction: executing action={action}, data={data}")

    # 1. Execute OS action first (independent of Fusion UI state)
    if action in ("open_file", "open_folder"):
        if data and os.path.exists(data):
            res = _OpenFile(data)
            _dbg_global(f"_HandleNotificationAction: _OpenFile({data}) returned {res}")
        else:
            _dbg_global(f"_HandleNotificationAction: path does not exist: '{data}'")

    # 2. Hide palette if requested or on action
    try:
        app = adsk.core.Application.get() if ("adsk" in sys.modules and hasattr(adsk, "core") and hasattr(adsk.core, "Application")) else None
        ui = getattr(app, "userInterface", None) if app else None
        if ui and hasattr(ui, "palettes") and ui.palettes:
            p = ui.palettes.itemById(palette_id)
            if p:
                p.isVisible = False
    except Exception as hide_exc:
        _dbg_global(f"_HandleNotificationAction hide palette note: {hide_exc}")


class _NotificationNavigationEventHandler(_NavigationEventHandlerBase):
    def __init__(self, palette_id):
        super().__init__()
        self.palette_id = palette_id

    def notify(self, args):
        try:
            url = getattr(args, "navigationURL", "")
            if "adsk" in sys.modules and hasattr(adsk, "core") and hasattr(adsk.core, "NavigationEventArgs"):
                try:
                    c = adsk.core.NavigationEventArgs.cast(args)
                    if c:
                        url = c.navigationURL
                except Exception:
                    pass

            _dbg_global(f"NotificationNavigationEventHandler notify: url={url}")

            if url and url.startswith("action://"):
                try:
                    args.launchExternally = False
                except Exception:
                    pass
                try:
                    if hasattr(args, "preventDefault"):
                        args.preventDefault = True
                except Exception:
                    pass

                p = urllib.parse.urlparse(url)
                action = (p.netloc or p.path).strip("/")
                q = urllib.parse.parse_qs(p.query)
                data = q.get("path", [""])[0] or q.get("data", [""])[0]
                _dbg_global(f"NotificationNavigationEventHandler parsed: action={action}, data={data}")
                _HandleNotificationAction(self.palette_id, action, data)
        except Exception as exc:
            _dbg_global(f"NotificationNavigationEventHandler exception: {exc}\n{traceback.format_exc()}")


class _NotificationHTMLEventHandler(_HTMLEventHandlerBase):
    def __init__(self, palette_id):
        super().__init__()
        self.palette_id = palette_id

    def notify(self, args):
        try:
            action = getattr(args, "action", "")
            data = getattr(args, "data", "")
            if "adsk" in sys.modules and hasattr(adsk, "core") and hasattr(adsk.core, "HTMLEventArgs"):
                try:
                    c = adsk.core.HTMLEventArgs.cast(args)
                    if c:
                        action = c.action
                        data = c.data
                except Exception:
                    pass

            _dbg_global(f"NotificationHTMLEventHandler notify: action={action}, data={data}")
            _HandleNotificationAction(self.palette_id, action, data)
        except Exception as exc:
            _dbg_global(f"NotificationHTMLEventHandler exception: {exc}")


def _BuildNotificationHTML(title, status_type, message, details, actions):
    """
    Generate self-contained HTML for non-modal Fusion toast notification.
    Note: Fusion 360's palettes.add2 requires the HTML string to strictly start
    with <html> (no <!DOCTYPE html> or leading whitespace).
    """
    icon_map = {
        'success': '✓',
        'warning': '⚠',
        'error': '✕',
    }
    color_map = {
        'success': '#2e7d32',
        'warning': '#ed6c02',
        'error': '#d32f2f',
    }
    bg_accent = color_map.get(status_type, '#2e7d32')
    icon = icon_map.get(status_type, '✓')

    action_buttons = []
    for label, action_name, action_data in actions:
        esc_data = html.escape(action_data, quote=True).replace('\\', '\\\\').replace("'", "\\'")
        nav_href = f"action://{action_name}?path={urllib.parse.quote(action_data)}"
        action_buttons.append(
            f'<a class="action-btn" href="{nav_href}" onclick="sendAction(event, \'{action_name}\', \'{esc_data}\')">{html.escape(label)}</a>'
        )
    actions_html = "\n".join(action_buttons)

    # Note: Must strictly begin with <html> (no <!DOCTYPE html>) for Fusion 360 C++ add2 parser
    html_content = f"""<html>
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #2b2b2b;
    color: #f0f0f0;
    margin: 0;
    padding: 12px 16px;
    font-size: 12px;
    line-height: 1.45;
    overflow-y: auto;
    overflow-x: hidden;
    user-select: none;
    -webkit-user-select: none;
  }}
  @media (prefers-color-scheme: light) {{
    body {{ background: #fdfdfd; color: #202020; border: 1px solid #d0d0d0; }}
  }}
  .toast-header {{
    display: flex;
    align-items: center;
    font-size: 13.5px;
    font-weight: 600;
    margin-bottom: 8px;
  }}
  .toast-icon {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background-color: {bg_accent};
    color: #ffffff;
    font-size: 12px;
    font-weight: bold;
    margin-right: 8px;
    flex-shrink: 0;
  }}
  .toast-title {{
    flex-grow: 1;
    font-weight: 600;
  }}
  .toast-body {{
    font-size: 12px;
    margin-bottom: 6px;
    color: #e0e0e0;
    word-break: break-word;
  }}
  @media (prefers-color-scheme: light) {{
    .toast-body {{ color: #333333; }}
  }}
  .toast-details {{
    font-size: 11px;
    color: #9e9e9e;
    margin-bottom: 12px;
    word-break: break-all;
  }}
  .toast-actions {{
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 4px;
  }}
  a.action-btn, button.action-btn {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: #3e3e3e;
    color: #ffffff !important;
    text-decoration: none !important;
    border: 1px solid #555555;
    padding: 6px 12px;
    border-radius: 4px;
    font-size: 11.5px;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
    user-select: none;
    -webkit-user-select: none;
  }}
  @media (prefers-color-scheme: light) {{
    a.action-btn, button.action-btn {{ background: #eeeeee; color: #111111 !important; border-color: #cccccc; }}
  }}
  a.action-btn:hover, button.action-btn:hover {{
    background: #007acc;
    border-color: #007acc;
    color: #ffffff !important;
  }}
</style>
<script>
  function sendAction(e, action, data) {{
    // 1. Try adsk.fusionSendData if available
    try {{
      if (typeof adsk !== 'undefined' && adsk && adsk.fusionSendData) {{
        adsk.fusionSendData(action, data);
      }} else if (window.adsk && window.adsk.fusionSendData) {{
        window.adsk.fusionSendData(action, data);
      }}
    }} catch (err) {{}}

    // 2. Direct location navigation to fire navigatingURL (universal fail-safe)
    try {{
      var navUrl = 'action://' + encodeURIComponent(action) + '?path=' + encodeURIComponent(data) + '&t=' + Date.now();
      window.location.href = navUrl;
    }} catch (err2) {{}}
  }}
  var dismissTimer = setTimeout(function() {{
    sendAction(null, 'dismiss', '');
  }}, 6000);
  document.addEventListener('mouseenter', function() {{
    if (dismissTimer) clearTimeout(dismissTimer);
  }});
  document.addEventListener('mouseleave', function() {{
    dismissTimer = setTimeout(function() {{
      sendAction(null, 'dismiss', '');
    }}, 2500);
  }});
</script>
</head>
<body>
  <div class="toast-header">
    <div class="toast-icon">{icon}</div>
    <div class="toast-title">{html.escape(title)}</div>
  </div>
  <div class="toast-body">{html.escape(message)}</div>
  {f'<div class="toast-details">{html.escape(details)}</div>' if details else ''}
  <div class="toast-actions">
    {actions_html}
  </div>
</body>
</html>"""
    return html_content.strip()


def _NotifyPostProcessResult(ui, cntFiles, cntSkipped, lstSkipped, outputFolder, verifyResults=None, is_fatal=False):
    """
    Provide non-modal feedback via Fusion statusMessage and non-modal Palette toast,
    matching Fusion native post-processing behavior without blocking modal message boxes.
    """
    global _notification_handler
    verifyResults = verifyResults or []

    # 1. Determine status type and headline
    has_errors = (cntFiles == 0 or is_fatal)
    has_collisions = any(bool(r.get("kinds")) for r in verifyResults)
    has_warnings = (cntSkipped > 0 or has_collisions or any(bool(r.get("warning")) for r in verifyResults))

    if has_errors:
        status_type = "error"
        title = "Post Processing Failed"
        msg = lstSkipped.strip() if lstSkipped else "No CAM operations posted."
    elif has_warnings:
        status_type = "warning"
        title = "Post Processing Completed with Warnings"
        if cntSkipped > 0:
            msg = f"{cntFiles} file(s) written. {cntSkipped} setup(s) skipped due to errors."
        else:
            msg = f"{cntFiles} file(s) written. Collision warnings detected during verification."
    else:
        status_type = "success"
        title = "Post Processing Completed Successfully"
        if len(verifyResults) == 1:
            r0 = verifyResults[0]
            prom = r0.get("promoted", 0) or 0
            msg = f"{r0['setup']}: {cntFiles} file written ({prom} rapid moves restored, 0 collisions)."
        else:
            msg = f"{cntFiles} file(s) successfully written."

    details = outputFolder if outputFolder else ""

    # 2. Log status summary to Application console (without flickering status bar)
    status_summary = f"[PostProcessAll] {title}: {msg}"
    try:
        if "adsk.core" in sys.modules and hasattr(adsk.core, "Application"):
            adsk.core.Application.get().log(status_summary)
    except Exception:
        pass

    # 3. Build Action buttons for the non-modal toast
    actions = []
    last_res = verifyResults[-1] if verifyResults else None
    if last_res:
        if last_res.get("cnc") and os.path.isfile(last_res["cnc"]):
            actions.append(("Open NC File", "open_file", last_res["cnc"]))
    if outputFolder and os.path.isdir(outputFolder):
        actions.append(("Open Folder", "open_folder", outputFolder))
    if last_res:
        if last_res.get("report") and os.path.isfile(last_res["report"]):
            actions.append(("View Report", "open_file", last_res["report"]))
        if last_res.get("png") and os.path.isfile(last_res["png"]):
            actions.append(("View Plot", "open_file", last_res["png"]))

    # 4. Display non-modal Palette toast if supported by ui
    if ui and hasattr(ui, "palettes") and ui.palettes is not None:
        html_text = _BuildNotificationHTML(title, status_type, msg, details, actions)
        tmp_html = os.path.join(tempfile.gettempdir(), "postprocess_toast.html")
        try:
            with open(tmp_html, "w", encoding="utf-8") as f:
                f.write(html_text)
        except Exception as fe:
            _dbg_global(f"_NotifyPostProcessResult write tmp_html note: {fe}")

        palette_w = 500
        palette_h = 220
        target_x = 940
        target_y = 680

        # Query macOS Fusion window bounds for pixel-perfect lower-right placement
        if sys.platform == "darwin":
            try:
                import subprocess
                cmd = 'tell application "System Events" to tell process "Autodesk Fusion" to repeat with w in windows\nif name of w contains "Autodesk Fusion" then\nreturn {position of w, size of w}\nend if\nend repeat'
                raw = subprocess.check_output(['osascript', '-e', cmd], timeout=0.4).decode().strip()
                parts = [int(p.strip()) for p in raw.split(',') if p.strip().lstrip('-').isdigit()]
                if len(parts) >= 4:
                    win_x, win_y, win_w, win_h = parts[0], parts[1], parts[2], parts[3]
                    target_x = max(20, win_x + win_w - palette_w - 24)
                    target_y = max(20, win_y + win_h - palette_h - 45)
                    _dbg_global(f"_NotifyPostProcessResult: macOS window bounds ({win_x},{win_y},{win_w},{win_h}) -> target ({target_x}, {target_y})")
            except Exception as e_mac:
                _dbg_global(f"_NotifyPostProcessResult: mac window query error: {e_mac}")
        else:
            app = adsk.core.Application.get() if ("adsk" in sys.modules and hasattr(adsk, "core")) else None
            if app and hasattr(app, "activeViewport") and app.activeViewport:
                vp = app.activeViewport
                try:
                    if hasattr(adsk.core, "Point2D") and hasattr(adsk.core.Point2D, "create"):
                        pt = vp.viewToScreen(adsk.core.Point2D.create(vp.width, vp.height))
                        if pt:
                            target_x = max(50, int(pt.x) - palette_w - 20)
                            target_y = max(50, int(pt.y) - palette_h - 20)
                except Exception:
                    pass

        palette = None

        # Check if existing notification palette exists and recreate cleanly
        existing = None
        try:
            existing = ui.palettes.itemById(constNotificationPaletteId)
        except Exception:
            existing = None

        if existing:
            try:
                _dbg_global(f"_NotifyPostProcessResult: cleaning up previous palette id={constNotificationPaletteId}")
                existing.deleteMe()
            except Exception as rex:
                _dbg_global(f"_NotifyPostProcessResult: palette cleanup error: {rex}")
            existing = None

        if not palette and os.path.isfile(tmp_html):
            if hasattr(ui.palettes, "add2"):
                try:
                    _dbg_global("_NotifyPostProcessResult: attempting ui.palettes.add2 with tmp_html")
                    palette = ui.palettes.add2(
                        constNotificationPaletteId,
                        "Post Processing",
                        tmp_html,
                        True,   # isVisible=True so native Qt window is allocated for setPosition/setSize
                        True,   # showCloseButton
                        False,  # isResizable
                        palette_w,
                        palette_h,
                    )
                except Exception as e_add2:
                    _dbg_global(f"_NotifyPostProcessResult: add2 with tmp_html failed: {e_add2}")

            if not palette and hasattr(ui.palettes, "add"):
                try:
                    _dbg_global("_NotifyPostProcessResult: attempting ui.palettes.add with tmp_html")
                    palette = ui.palettes.add(
                        constNotificationPaletteId,
                        "Post Processing",
                        tmp_html,
                        True,
                        True,
                        False,
                        palette_w,
                        palette_h,
                    )
                except Exception as e_add:
                    _dbg_global(f"_NotifyPostProcessResult: add with tmp_html failed: {e_add}")

        if palette:
            _dbg_global(f"_NotifyPostProcessResult: palette active (id={constNotificationPaletteId})")
            try:
                palette.isVisible = True
            except Exception:
                pass

            try:
                if "adsk.core" in sys.modules:
                    if hasattr(adsk.core, "PaletteDockingOptions") and hasattr(adsk.core.PaletteDockingOptions, "PaletteDockOptionsNone"):
                        palette.dockingOption = adsk.core.PaletteDockingOptions.PaletteDockOptionsNone
                    if hasattr(adsk.core, "PaletteDockingStates") and hasattr(adsk.core.PaletteDockingStates, "PaletteDockStateFloating"):
                        palette.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateFloating
            except Exception as dock_exc:
                _dbg_global(f"_NotifyPostProcessResult docking state note: {dock_exc}")

            try:
                res_size = palette.setSize(palette_w, palette_h)
                _dbg_global(f"_NotifyPostProcessResult: palette.setSize({palette_w}, {palette_h}) returned {res_size}")
            except Exception as size_exc:
                _dbg_global(f"_NotifyPostProcessResult: setSize note: {size_exc}")

            try:
                res_pos = palette.setPosition(target_x, target_y)
                _dbg_global(f"_NotifyPostProcessResult: palette.setPosition({target_x}, {target_y}) returned {res_pos}")
            except Exception as pos_exc:
                _dbg_global(f"_NotifyPostProcessResult: setPosition failed: {pos_exc}")

            # Remove any stale notification handlers from global handlers
            global _palette_handlers, handlers
            for h in _palette_handlers:
                if h in handlers:
                    try:
                        handlers.remove(h)
                    except ValueError:
                        pass
            _palette_handlers = []

            # 1. Navigation Event Handler (handles action:// links from <a> or window.location.href)
            if hasattr(palette, "navigatingURL") and hasattr(palette.navigatingURL, "add"):
                try:
                    nav_handler = _NotificationNavigationEventHandler(constNotificationPaletteId)
                    palette.navigatingURL.add(nav_handler)
                    _palette_handlers.append(nav_handler)
                    handlers.append(nav_handler)
                    _dbg_global("_NotifyPostProcessResult: navigatingURL handler attached successfully")
                except Exception as n_exc:
                    _dbg_global(f"_NotifyPostProcessResult: navigatingURL attach note: {n_exc}")

            # 2. HTML Event Handler (handles adsk.fusionSendData)
            if hasattr(palette, "incomingFromHTML") and hasattr(palette.incomingFromHTML, "add"):
                try:
                    html_handler = _NotificationHTMLEventHandler(constNotificationPaletteId)
                    palette.incomingFromHTML.add(html_handler)
                    _palette_handlers.append(html_handler)
                    handlers.append(html_handler)
                    _dbg_global("_NotifyPostProcessResult: incomingFromHTML handler attached successfully")
                except Exception as h_exc:
                    _dbg_global(f"_NotifyPostProcessResult: incomingFromHTML attach note: {h_exc}")
        else:
            _dbg_global("_NotifyPostProcessResult: failed to create palette through all attempts")




def ShowVerifyResults(ui):
    """
    Log verification summaries to Application log without modal dialog interruption.
    Diagnostic details and collision breakdowns are saved to the XXX_report.md files.
    """
    _dbg_global(f"ShowVerifyResults called: verifyResults count={len(verifyResults)}")
    if not verifyResults:
        return
    for r in verifyResults:
        total_cols = sum(r["kinds"].values()) if r.get("kinds") else 0
        if r.get("error"):
            status = "FAILED: " + r["error"]
        elif r.get("warning") or total_cols > 0:
            warn_str = r.get("warning") or f"{total_cols} collision(s) detected"
            status = f"WARNING: {warn_str}"
        else:
            status = "OK"
        msg = f"[PostProcessAll] Verification for '{r['setup']}': {status} | NC: {r.get('cnc')} | Report: {r.get('report')}"
        _dbg_global(msg)
        try:
            adsk.core.Application.get().log(msg)
        except Exception:
            pass


def _OpenFile(path):
    """Open a file in the OS viewer. Returns None on success or an error string."""
    _dbg_global(f"_OpenFile: requested for '{path}' (exists={os.path.exists(path)})")
    try:
        if os.name != "posix":
            os.startfile(path)
            _dbg_global(f"_OpenFile: os.startfile succeeded for '{path}'")
            return None
        # Strip Fusion's embedded-Python/dyld variables so LaunchServices helpers start cleanly
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "DYLD_"))}
        attempts = [["open", path]]
        if path.lower().endswith(".png"):
            attempts.insert(0, ["open", "-a", "Preview", path])
        last = ""
        for cmd in attempts:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
            if proc.returncode == 0:
                _dbg_global(f"_OpenFile: command {cmd} succeeded")
                return None
            last = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
            _dbg_global(f"_OpenFile: command {cmd} failed with {last}")
        return last
    except Exception as exc:
        err = f"{exc}\n{traceback.format_exc()}"
        _dbg_global(f"_OpenFile: exception: {err}")
        return str(exc)


def _PostProcessUnifiedPipeline(fname, setup, setupFolder, docSettings, program, path, fileExt, target_operations=None, progress_cb=None):
    """
    Unified 2-pass Context-Dump & Single-Pass Replay pipeline for Milling and Turning setups:
    Pass 1 (Context Extraction): Post each single-tool batch inside Fusion using universal_dumper.cps
            (inheriting vendor BASE_POST settings) to emit batch_i.json and capture bootstrap .cnc.
    Pass 2 (Merge, Universal Domain Extraction, Unified Verification & Single-Pass Replay):
            - Merge all batch_i.json files into a unified multi-section context dump.
            - Extract domain WorkpieceDefinition & tools from context:
              * Turning: AxisymmetricStock2D + exact kernel SVG profiles.
              * Milling: BoxStock3D + parametric MillToolGeometry.
            - Verify toolpath:
              * Turning: Lathe2DIPWVerifier (dynamic 2D IPW simulation + chuck guard).
              * Milling: MillHeightfieldVerifier (3D Heightfield Grid IPW simulation with gouge & collision detection).
            - Universal collision check (RAPID_INTO_STOCK, HOLDER_STRIKE, FIXTURE_COLLISION, SHANK_RUB).
              Issues warnings, logs diagnostics, and highlights collisions while continuing final NC generation.
            - If restore_rapid is enabled, stamp promotions onto merged context.
            - Single-pass replay of unmodified vendor post via universal_replayer.cps.
            - Generate summary report (_report.md) and record verify results for UI.
    """
    _dbg = _dbg_global
    _notify_p = progress_cb or (lambda msg, frac: True)
    _notify_p("Initializing pipeline...", 0.02)
    is_turning = (setup.operationType == adsk.cam.OperationTypes.TurningOperation)
    _dbg(f"_PostProcessUnifiedPipeline entered for {fname}, path={path}, is_turning={is_turning}")

    # 0. Ensure Add-In modules are on sys.path and reloaded in Fusion's persistent interpreter
    _EnsureLocalModulesLoaded()

    base_post_path = _ResolveBasePostPath(program, is_turning=is_turning)
    if not base_post_path or not os.path.isfile(base_post_path):
        prog_name = getattr(program, "name", "None")
        post_cfg = getattr(program, "postConfiguration", None)
        post_name = getattr(post_cfg, "name", None) or getattr(post_cfg, "description", None) or "None"
        setup_type = "Turning" if is_turning else "Milling"
        if is_turning is False and "centroid_turning" in str(post_name).lower():
            return (
                f"NC Program '{prog_name}' is configured with turning post '{post_name}', "
                f"which does not support milling toolpaths. "
                f"Please select or configure a milling NC Program for setup '{setup.name}'."
            )
        if is_turning is True and any(m in str(post_name).lower() for m in ("tormach", "mill", "router")):
            return (
                f"NC Program '{prog_name}' is configured with milling post '{post_name}', "
                f"which does not support turning toolpaths. "
                f"Please select or configure a turning NC Program for setup '{setup.name}'."
            )
        return (
            f"Could not locate machine post processor for {setup_type} setup '{setup.name}' "
            f"(NC Program '{prog_name}', post '{post_name}'). "
            f"Please verify the post processor configuration in your NC Program."
        )
    post_props = _ExtractPostProperties(program)
    _dbg(f"_PostProcessUnifiedPipeline: base_post_path={base_post_path}, post_props={post_props}")

    # Ensure output path never uses .json extension
    if path.lower().endswith(".json"):
        ext_fix = fileExt if (fileExt and fileExt.lower().lstrip(".") != "json") else ".cnc"
        if not ext_fix.startswith("."):
            ext_fix = "." + ext_fix
        path = os.path.splitext(path)[0] + ext_fix

    # 2. Partition setup operations into single-tool batches (mandatory for Personal Edition compatibility)
    cand_ops = target_operations if target_operations is not None else getattr(setup, "allOperations", [])
    cand_list = _extract_cam_collection(cand_ops)

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
            if not docSettings.get("combineTool", True) or t_num != curTool:
                break
            opList.append(next_op)
            i += 1
        batches.append((curTool, opList))

    _dbg(f"_PostProcessUnifiedPipeline: partitioned into {len(batches)} batches: {[b[0] for b in batches]}")
    if not batches:
        return "No active operations to post process."

    tmp_dir = tempfile.mkdtemp(prefix="unified_pipe_")
    tmp_prog = None
    try:
        # -------------------------------------------------------------
        # PASS 1: Extract full post context (.json) per single-tool batch
        # Use a temporary NCProgram so the user's NCProgram (Rapid_Restore)
        # retains its selected vendor post processor and custom postParameters.
        # -------------------------------------------------------------
        app = adsk.core.Application.get()
        cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType(constCAMProductId))
        nc_input = cam.ncPrograms.createInput()
        nc_input.displayName = "_Tmp_Unified_Context_Dumper"
        tmp_prog = cam.ncPrograms.add(nc_input)
        try:
            tmp_prog.parameters.itemByName("nc_program_createInBrowser").value.value = False
        except Exception:
            pass

        dumper_post_obj = _GetPostConfigObj(constDumperScript)
        batch_json_files = []
        bootstrap_cnc = os.path.join(tmp_dir, "bootstrap.intermediate.cnc")
        opts = adsk.cam.NCProgramPostProcessOptions.create()
        if hasattr(opts, "isFailOnToolNumberDuplication"):
            opts.isFailOnToolNumberDuplication = False

        n_batches = max(1, len(batches))
        for b_idx, (t_num, op_list) in enumerate(batches):
            b_frac = 0.08 + 0.32 * (b_idx / n_batches)
            _notify_p(f"Pass 1: Dumping toolpath batch {b_idx + 1} of {n_batches} (Tool {t_num})...", b_frac)
            b_prefix = f"batch_{b_idx}"
            b_json = os.path.join(tmp_dir, f"{b_prefix}.json")

            os.environ["BASE_POST"] = os.path.abspath(base_post_path)
            if b_idx == 0:
                os.environ["TARGET_INTERMEDIATE_CNC"] = bootstrap_cnc
                _WriteDumperConfig(
                    base_post_path=base_post_path,
                    target_cnc_path=bootstrap_cnc,
                    post_properties=post_props,
                )
            else:
                os.environ.pop("TARGET_INTERMEDIATE_CNC", None)
                _WriteDumperConfig(
                    base_post_path=base_post_path,
                    target_cnc_path=None,
                    post_properties=post_props,
                )

            tmp_prog.postConfiguration = dumper_post_obj
            tmp_prog.operations = op_list
            AssignOutputFolder(tmp_prog.parameters, tmp_dir)
            tmp_prog.parameters.itemByName("nc_program_filename").value.value = b_prefix
            tmp_prog.parameters.itemByName("nc_program_openInEditor").value.value = False

            _dbg(f"Pass 1: Posting batch {b_idx} (Tool {t_num}, {len(op_list)} ops) with universal_dumper.cps...")
            post_ok = bool(tmp_prog.postProcess(opts))
            time.sleep(constPostLoopDelay)

            if not os.path.isfile(b_json):
                for f in os.listdir(tmp_dir):
                    if f.startswith(b_prefix) and f.endswith(".json"):
                        b_json = os.path.join(tmp_dir, f)
                        break

            if not os.path.isfile(b_json):
                _dbg(f"Pass 1: Failed to produce {b_json}!")
                return f"Failed to dump post context for batch {b_idx} (Tool {t_num})."

            if b_idx == 0 and not os.path.isfile(bootstrap_cnc):
                fallback_inter = b_json + ".intermediate.cnc"
                if os.path.isfile(fallback_inter):
                    shutil.copy2(fallback_inter, bootstrap_cnc)

            batch_json_files.append(b_json)

        # -------------------------------------------------------------
        # PASS 2: Merge Contexts, Universal Domain Extraction & Single-Pass Replay
        # -------------------------------------------------------------
        _notify_p("Pass 2: Merging toolpath context...", 0.42)
        from context_parser import (
            merge_context_dumps,
            parse_context_dump,
            apply_promotions_to_context,
            extract_workpiece_from_context,
            extract_tools_from_context,
        )
        from geometry import CollisionKind
        from pipeline import PipelineOrchestrator

        combined_ctx = merge_context_dumps(batch_json_files)
        # Override programName with target setup filename and forward custom post properties
        combined_ctx["programName"] = str(fname)
        if post_props:
            combined_ctx["postProperties"] = post_props

        # Universal domain extraction: WorkpieceDefinition & Tools
        fallback_wp = _ExtractWorkpieceFromSetup(setup) if is_turning else None
        workpiece = extract_workpiece_from_context(combined_ctx, fallback_workpiece=fallback_wp, is_turning=is_turning)
        tools_by_op = extract_tools_from_context(combined_ctx, is_turning=is_turning)
        if is_turning and not tools_by_op:
            return "No lathe operations with valid tools found in context dump."

        combined_segments = parse_context_dump(combined_ctx)
        _dbg(f"Pass 2: Combined {len(combined_segments)} motion segments across {len(batches)} batches.")

        base_no_ext = os.path.splitext(path)[0]
        report_path = base_no_ext + "_report.md"
        plot_path = base_no_ext + ".png"

        # Instantiate verification engine based on setup operation type
        _notify_p(f"Pass 2: Simulating stock & collisions ({'turning' if is_turning else 'milling'})...", 0.50)
        if is_turning:
            from geometry import Lathe2DIPWVerifier
            verifier = Lathe2DIPWVerifier(tool_scale_to_mm=1.0)
        else:
            from geometry import MillHeightfieldVerifier
            verifier = MillHeightfieldVerifier()

        report = verifier.verify_toolpath(combined_segments, tools_by_op, workpiece)

        # Generate verification plot (.png)
        if plot_path:
            _notify_p("Pass 2: Generating verification plot & report...", 0.70)
            try:
                from geometry.plotter import generate_verification_plot, generate_mill_verification_plot
                is_inch = (combined_ctx.get("unit", 1) == 0)
                if is_turning:
                    generate_verification_plot(
                        workpiece=workpiece,
                        final_ipw=getattr(verifier, "ipw", None),
                        chuck_obstacle=getattr(verifier, "chuck_obstacle", None),
                        segments=combined_segments,
                        report=report,
                        tools_by_op=tools_by_op,
                        out_png_path=plot_path,
                        unit_is_inch=is_inch,
                    )
                else:
                    generate_mill_verification_plot(
                        workpiece=workpiece,
                        grid=getattr(verifier, "grid", None),
                        segments=combined_segments,
                        report=report,
                        tools_by_op=tools_by_op,
                        out_png_path=plot_path,
                        unit_is_inch=is_inch,
                    )
                _dbg(f"Pass 2: Generated verification plot: {plot_path}")
            except Exception as pl_exc:
                _dbg(f"Pass 2: generate verification plot exception: {pl_exc}")

        orchestrator = PipelineOrchestrator(
            dumper_cps=constDumperScript,
            replayer_cps=constReplayerScript,
        )

        # Universal collision check: issue warnings and record collision metrics while continuing NC generation
        fatal_cols = [
            c for c in report.collisions
            if c.kind in (CollisionKind.RAPID_INTO_STOCK, CollisionKind.HOLDER_STRIKE, CollisionKind.FIXTURE_COLLISION, CollisionKind.SHANK_RUB)
        ]
        warning_msg = None
        if fatal_cols:
            warning_msg = f"{len(fatal_cols)} collision(s) detected"
            _dbg(f"Pass 2: WARNING: {warning_msg} in setup {setup.name}! Continuing with final NC code generation.")
            try:
                adsk.core.Application.get().log(f"[PostProcessAll] WARNING: {warning_msg} in setup {setup.name}!")
            except Exception:
                pass

        # Stamp promoted rapid flags directly onto merged context
        do_promote = bool(docSettings.get("restore_rapid", True))
        promoted_total = 0
        if do_promote and report.promoted_vectors:
            promoted_total = apply_promotions_to_context(
                combined_ctx, report.promoted_vectors
            )

        # Single-pass replay of unmodified vendor post across all merged sections
        _notify_p("Pass 2: Emitting final NC program...", 0.85)
        _dbg(f"Pass 2: Replaying merged context ({len(combined_ctx.get('sections', []))} sections, {promoted_total} promoted moves) with universal_replayer.cps...")
        final_gcode = orchestrator.replay_combined_context(
            combined_context=combined_ctx,
            base_post_path=base_post_path,
            bootstrap_cnc_path=bootstrap_cnc,
            final_cnc_path=path,
        )
        _dbg(f"Pass 2: Successfully emitted combined NC program at {path}")

        summary_md = orchestrator._format_summary_report(
            report, final_gcode=final_gcode, base_post_path=base_post_path, restored_count=promoted_total
        )
        with open(report_path, "w", encoding="utf-8") as rf:
            rf.write(summary_md)

        _notify_p("Setup completed.", 1.0)

        _RecordVerifyResult(
            setup=setup,
            cncPath=path,
            reportText=summary_md,
            reportPath=report_path,
            pngPath=plot_path,
            promoted=promoted_total,
            spikes=len(report.mrr_spikes),
            volume_mm3=report.total_volume_removed_mm3,
            collisions=report.collisions,
            error=None,
            warning=warning_msg,
        )

        return None

    except Exception as exc:
        _dbg(f"_PostProcessUnifiedPipeline exception: {traceback.format_exc()}")
        return f"PostProcess pipeline error: {str(exc)}"
    finally:
        _ClearDumperConfig()
        if tmp_prog is not None:
            try:
                tmp_prog.deleteMe()
            except Exception:
                pass
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


# Backwards compatibility alias
_PostProcessLathePipeline = _PostProcessUnifiedPipeline


def PostProcessSetup(fname, setup, setupFolder, docSettings, program, target_operations=None, progress_cb=None):
    ui = None
    retVal = "Fusion reported an exception"

    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface
        doc = app.activeDocument
        cam = adsk.cam.CAM.cast(doc.products.itemByProductType(constCAMProductId))
        parameters = program.parameters

        # Verify file name is valid by creating it now
        fileExt = parameters.itemByName("nc_program_nc_extension").value.value
        if fileExt and fileExt.lower().lstrip(".") == "json":
            fileExt = ".cnc"
            try:
                parameters.itemByName("nc_program_nc_extension").value.value = ".cnc"
            except Exception:
                pass
        path = setupFolder + "/" + fname + fileExt
        try:
            pathlib.Path(setupFolder).mkdir(parents=True, exist_ok=True)
            if os.path.exists(path):
                if not os.access(path, os.W_OK):
                    raise PermissionError(f"File {path} is not writable")
            else:
                with open(path, "w", encoding="utf8"):
                    pass
                os.remove(path)
        except Exception as exc:
            return "Unable to create output file '" + path + "'. Make sure the setup name is valid as a file name."
        
        # Make sure toolpaths are valid
        ops_to_check = target_operations if target_operations is not None else [setup]
        for item in ops_to_check:
            if not cam.checkToolpath(item):
                if progress_cb:
                    progress_cb("Generating missing toolpath...", 0.02)
                genStat = cam.generateToolpath(item)
                while not genStat.isGenerationCompleted:
                    time.sleep(.1)
                    if "adsk" in sys.modules and hasattr(adsk, "doEvents"):
                        try:
                            adsk.doEvents()
                        except Exception:
                            pass

        _dbg = _dbg_global
        _dbg(f"PostProcessSetup called: fname={fname}, setup={setup.name}, opType={setup.operationType}, target_ops_cnt={len(target_operations) if target_operations is not None else 'all'}")

        # Unified 2-pass dump & single-pass replay pipeline for all setups
        return _PostProcessUnifiedPipeline(fname, setup, setupFolder, docSettings, program, path, fileExt, target_operations=target_operations, progress_cb=progress_cb)

    except Exception as exc:
        if ui:
            retVal += " " + traceback.format_exc()
        return retVal
