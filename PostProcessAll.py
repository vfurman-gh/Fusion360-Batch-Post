#Author-Tim Paterson
#Description-Post process all CAM setups, using the setup name as the output file name.

import adsk.core, adsk.fusion, adsk.cam, traceback, shutil, json, os, os.path, time, re, pathlib, enum, tempfile, subprocess, io, glob, math

# Version number of settings as saved in documents and settings file
# update this whenever settings content changes
version = 13

# Initial default values of settings
defaultSettings = {
    "version" : version,
    "ncProgram": "",
    "output" : "",
    "sequence" : True,
    "twoDigits" : False,
    "delFiles" : False,
    "delFolder" : False,
    "splitSetup" : False,
    "combineTool" : False,
    "fastZ" : True,
    "fastZHeuristic" : True,
    "verifyGcode" : True,
    "toolChange" : "M9 G30",
    "numericName" : False,
    "endCodes" : "M5 M9 M30",
    "onlySelected" : False,
    # Groups are expanded or not
    "groupPersonal" : True,
    "groupPost" : False,
    "groupAdvanced" : False,
    "groupRename" : False,
    # Retry policy
    "initialDelay" : 0.2,
    "postRetries" : 3
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
constBodyTmpFile = "gcodeBody"
constOpTmpFile = "8910"   # in case name must be numeric
constRapidZgcode = 'G00 Z{} (Changed from: "{}")\n'
constRapidXYgcode = 'G00 {} (Changed from: "{}")\n'
constFeedZgcode = 'G01 Z{} F{} (Changed from: "{}")\n'
constFeedXYgcode = 'G01 {} F{} (Changed from: "{}")\n'
constFeedXYZgcode = 'G01 {} Z{} F{} (Changed from: "{}")\n'
constAddFeedGcode = " F{} (Feed rate added)\n"
constMotionGcodeSet = {0,1,2,3,33,38,73,76,80,81,82,84,85,86,87,88,89}
constHomeGcodeSet = {28, 30}
constLineNumInc = 5
constNcProgramName = "PostProcessAll NC Program"
constAddonDir = os.path.dirname(os.path.abspath(__file__))
constWrapperScript = os.path.join(constAddonDir, "universal_wrapper.cps")
constAptExtractorScript = os.path.join(constAddonDir, "apt_extractor_wrapper.cps")
constPackagesDir = os.path.join(constAddonDir, "packages")

# Tool tip text
toolTip = (
    "Post process all setups into G-code for your machine.\n\n"
    "The name of the setup is used for the name of the output "
    "file adding the appropriate extension. A colon (':') in the name indicates "
    "the preceding portion is the name of a subfolder. Multiple "
    "colons can be used to nest subfolders. Spaces around colons "
    "are removed.\n\n"
    "Setups within a folder are optionally preceded by a "
    "sequence number. This identifies the order in which the "
    "setups appear. The sequence numbers for each folder begin "
    "with 1."
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
                # never allow delFiles or delFolder to default to True
                self.default["delFiles"] = False
                self.default["delFolder"] = False
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
        # never allow delFiles or delFolder to default to True
        self.default["delFiles"] = False
        self.default["delFolder"] = False
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
        if "homeEndsOp" in dst:
            if dst["homeEndsOp"] and not ("endCodes" in dst):
                dst["endCodes"] = "M5 M9 M30 G28 G30"
            del dst["homeEndsOp"]
        if "fastZHeuristic" not in dst:
            dst["fastZHeuristic"] = dst.get("fastZ", True)
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
        cmdDef = cmdDefs.addButtonDefinition(constCmdDefId, constCmdName, toolTip, "resources/Command")
        
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


def CountOutputFolderFiles(folder, limit, fileExt):
    cntFiles = 0
    cntNcFiles = 0
    for path, dirs, files in os.walk(folder):
        for file in files:
            if file.endswith(fileExt):
                cntNcFiles += 1
            else:
                cntFiles += 1
        if cntFiles > limit:
            return "many files that are not G-code"
        if cntNcFiles > limit * 1.5:
            return "many more G-code files than are produced by this design"
    return None


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


def GetSetups(cam, settings, setups):
    if len(setups) == 0 or not settings["onlySelected"]:
        setups = []
        # move all setups into a list
        for setup in cam.setups:
            setups.append(setup)
    return setups


def GetSetupTypes(cam, selectedSetups, onlySelected):
    """Determine if non-suppressed turning and/or milling setups exist in the target set."""
    targetSetups = selectedSetups if (onlySelected and len(selectedSetups) != 0) else cam.setups
    hasTurning = any(
        not s.isSuppressed and s.operationType == adsk.cam.OperationTypes.TurningOperation
        for s in targetSetups
    )
    hasMilling = any(
        not s.isSuppressed and s.operationType == adsk.cam.OperationTypes.MillingOperation
        for s in targetSetups
    )
    return hasTurning, hasMilling


def GetNcProgram(cam, settings):
    for program in cam.ncPrograms:
        if program.name == settings["ncProgram"]:
            return program
    return cam.ncPrograms.item(0)


def RenameSetups(settings, setups, find, replace, isRegex):
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface
        doc = app.activeDocument
        cam = adsk.cam.CAM.cast(doc.products.itemByProductType(constCAMProductId))
        setups = GetSetups(cam, settings, setups)
        
        for setup in setups:
            if isRegex:
                newName = re.sub(find, replace, setup.name)
            else:
                if find == "":
                    # special case, prepend
                    newName = replace + setup.name
                else:
                    newName = setup.name.replace(find, replace)

            if setup.name != newName:
                setup.name = newName

        # Save settings in document attributes
        settingsMgr.SaveSettings(doc.attributes, settings)

    except:
        pass


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

            # See if we're doing only selected setups and whether any turning or milling setups exist
            selectedSetups = []
            for setup in cam.setups:
                if setup.isSelected:
                    selectedSetups.append(setup)
            hasTurningSetup, hasMillingSetup = GetSetupTypes(
                cam, selectedSetups, docSettings.get("onlySelected", False)
            )

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
            onExecute = CommandExecuteHandler(docSettings, selectedSetups)
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

            # check box to use only selected setups
            input = inputs.addBoolValueInput("onlySelected", 
                                             "Only selected setups", 
                                             True, 
                                             "", 
                                             docSettings["onlySelected"])
            input.tooltip = "Only Process Selected Setups"
            input.tooltipDescription = (
                "Only setups selected in the browser will be processed. Note "
                "that a selected setup will be highlighted, not simply activated. "
                "Selecting individual operations within a setup has no effect."
            )
            input.isEnabled = len(selectedSetups) != 0

            # check box to delete existing files
            input = inputs.addBoolValueInput("delFiles", 
                                             "Delete existing files", 
                                             True, 
                                             "", 
                                             docSettings["delFiles"])
            input.tooltip = "Delete Existing Files in Each Folder"
            input.tooltipDescription = (
                "Delete all files in each output folder before post processing. "
                "This will help prevent accumulation of G-code files which are "
                "no longer used."
                "<p>For example, you could decide to add sequence numbers after "
                "already post processing without them. If this option is not "
                "checked, you will have two of each file, a newer one with a "
                "sequence number and older one without. With this option checked, "
                "all previous files will be deleted so only current results will "
                "be present.</p>"
                "<p>This option will only delete the files in folders in which new "
                "G-code files are being written. If you change the name of a "
                "folder, for example, it will not be deleted.</p>")

            # check box to delete entire output folder
            input = inputs.addBoolValueInput("delFolder", 
                                             "Delete output folder", 
                                             True, 
                                             "", 
                                             docSettings["delFolder"] and docSettings["delFiles"])
            input.isEnabled = docSettings["delFiles"] # enable only if delete existing files
            input.tooltip = "Delete Entire Output Folder First"
            input.tooltipDescription = (
                "Delete the entire output folder before post processing. This "
                "deletes all files and subfolders regardless of whether or not "
                "new G-code files are written to a particular folder."
                "<p><b>WARNING!</b> Be absolutely sure the output folder is set "
                "correctly before selecting this option. Run the command once "
                "before setting this option and verify the results are in the "
                "correct folder. An incorrect setting of the output folder with "
                "this option selected could result in unintentionally wiping out "
                "a vast number of files.</p>")

            # check box to prepend sequence numbers
            input = inputs.addBoolValueInput("sequence", 
                                             "Prepend sequence number", 
                                             True, 
                                             "", 
                                             docSettings["sequence"])
            input.tooltip = "Add Sequence Numbers to Name"
            input.tooltipDescription = (
                "Begin each file name with a sequence number. The numbering "
                "represents the order that the setups appear in the browser tree. "
                "Each folder has its own sequence numbers starting with 1.")

            # check box to select 2-digit sequence numbers
            input = inputs.addBoolValueInput("twoDigits", 
                                             "Use 2-digit numbers", 
                                             True, 
                                             "", 
                                             docSettings["twoDigits"])
            input.isEnabled = docSettings["sequence"] # enable only if using sequence numbers
            input.tooltip = "Use 2-Digit Sequence Numbers"
            input.tooltipDescription = (
                "Sequence numbers 0 - 9 will have a leading zero added, becoming"
                '"01" to "09". This could be useful for formatting or sorting.')

            # "Personal Use" version
            # check box to split up setup into individual operations
            inputGroup = inputs.addGroupCommandInput("groupPersonal", "Personal Use")
            input = inputGroup.children.addBoolValueInput("splitSetup",
                                                          "Use individual operations",
                                                          True,
                                                          "",
                                                          docSettings["splitSetup"])
            input.tooltip = "Split Setup Into Individual Operations"
            input.tooltipDescription = (
                "Generate output for each operation individually. This is usually "
                "REQUIRED when using Fusion for Personal Use, because tool "
                "changes are not allowed. The individual operations will be "
                "grouped back together into the same file, eliminating this "
                "limitation. You will get an error if there is a tool change "
                "in a setup and this options is not selected.")

            # check box to combine operation that use the same tool
            input = inputGroup.children.addBoolValueInput("combineTool",
                                                          "Combine operations using same tool",
                                                          True,
                                                          "",
                                                          docSettings["combineTool"])
            input.isEnabled = docSettings["splitSetup"] # enable only if using individual operations
            input.tooltip = "Combine Consecutive Operations That Use the Same Tool"
            input.tooltipDescription = (
                "If consecutive operations use the same tool, have Fusion generate "
                "their output together. This can optimize G-code for some routers. "
                "However, it will cause the logic that restores rapid moves to also "
                "treat it as one operation, which can have negative effects if the "
                "feed heights for the operations are different.")

            # text box as a label for tool change command
            input = inputGroup.children.addTextBoxCommandInput("toolLabel", 
                                                               "", 
                                                               "G-code for tool change:",
                                                               1,
                                                               True)
            input.isFullWidth = True
            label = input

            # enter G-code for tool change
            input = inputGroup.children.addStringValueInput("toolChange", "", docSettings["toolChange"])
            input.isEnabled = docSettings["splitSetup"] # enable only if using individual operations
            input.isFullWidth = True
            input.tooltip = "G-code to Precede Tool Change"
            input.tooltipDescription = (
                "Allows inserting a line of code before tool changes. For example, "
                "you might want M5 (spindle stop), M9 (coolant stop), and/or G28 or G30 "
                "(return to home). The code will be placed on the line before the "
                "tool change. You can get mulitple lines by separating them with "
                "a colon (:)."
                "<p>If you want a line number, just put a dummy line number in front. "
                "If you use the colon to get multiple lines, only put the dummy line "
                "number on the first line. For example, <b>N10 M9:G30</b> will give "
                "you two lines, both with properly sequenced line numbers.</p>"
            )
            label.tooltip = input.tooltip
            label.tooltipDescription = input.tooltipDescription
           
            # text box as a label for operation end commands
            input = inputGroup.children.addTextBoxCommandInput("endLabel", 
                                                               "", 
                                                               "G-codes that mark ending sequence:",
                                                               1,
                                                               True)
            input.isFullWidth = True
            label = input

            # enter G-codes for end of operation
            input = inputGroup.children.addStringValueInput("endCodes", "", docSettings["endCodes"])
            input.isEnabled = docSettings["splitSetup"] # enable only if using individual operations
            input.isFullWidth = True
            input.tooltip = "G-codes That Mark the Ending Sequence"
            input.tooltipDescription = (
                "To combine operations generated individually, the ending sequence "
                "(which should only appear once) must be found. This entry is the "
                "list of G-codes that start this ending sequence. For example, M30 "
                "(end program) would normally be here, but it may not be the first "
                "G-code of the ending sequence. M5 (spindle stop), M9 (coolant "
                "stop) and G28/G30 (move home) are also candidates, but you should "
                "look at the code from your post processor to determine what "
                "will work in your case. Any one of the G-codes you enter here "
                "will mark the start of ending sequence."
            )
            label.tooltip = input.tooltip
            label.tooltipDescription = input.tooltipDescription
           
            # check box to enable restoring rapid moves via 2D IPW verifier (Turning setups only)
            input = inputGroup.children.addBoolValueInput("fastZ",
                                                          "Restore rapid moves (stock simulation)",
                                                          True,
                                                          "",
                                                          docSettings["fastZ"])
            input.isEnabled = hasTurningSetup
            input.tooltip = "Restore Rapid Moves via 2D IPW Stock Simulation (Turning Only)"
            input.tooltipDescription = (
                "For Turning setups, analyze the generated lathe G-code with 2D "
                "In-Process Workpiece (IPW) simulation, promote collision-free G1 "
                "air moves (with safety clearance margin) to G0 rapids, and re-verify "
                "the modified G-code in Pass 2. Ignored for Milling setups.")

            # check box to enable restoring rapid moves via fast_z heuristics (Milling setups only)
            input = inputGroup.children.addBoolValueInput("fastZHeuristic",
                                                          "Restore rapid moves (fast_z heuristics)",
                                                          True,
                                                          "",
                                                          docSettings.get("fastZHeuristic", docSettings.get("fastZ", True)))
            input.isEnabled = hasMillingSetup and docSettings["splitSetup"]
            input.tooltip = "Restore Rapid Moves via Fast-Z Heuristics (Milling Only)"
            input.tooltipDescription = (
                "For Milling setups using individual operations, analyze G-code to find moves "
                "at or above the feed height and replace them with rapid (G0) moves.<br>"
                "Requires 'Use individual operations' to be checked."
                "<p><b>WARNING!</b> Review G-code to verify accuracy. Comments indicate the changes.</p>")

            # check box to run 2D collision verification & MRR analysis (Turning setups only)
            input = inputGroup.children.addBoolValueInput("verifyGcode",
                                                          "Verify Lathe G-code & plot MRR",
                                                          True,
                                                          "",
                                                          docSettings.get("verifyGcode", True) or docSettings["fastZ"])
            input.isEnabled = hasTurningSetup
            input.tooltip = "Verify Lathe G-Code & Plot MRR Time-Series (Turning Only)"
            input.tooltipDescription = (
                "For Turning setups, run 2D Lathe G-code collision verification "
                "(Insert, Holder, Stock IPW, Chuck), export the 3D Material Removal "
                "Rate (MRR) time-series CSV, and generate the 2-panel verification "
                "& MRR plot. Ignored for Milling setups.")
           
            inputGroup.isExpanded = docSettings["groupPersonal"]

            # Rename
            inputGroup = inputs.addGroupCommandInput("groupRename", "Rename Setups")

            # check box to use regular expressions
            input = inputGroup.children.addBoolValueInput("regex",
                                                          "Use Python regular expressions",
                                                          True,
                                                          "",
                                                          False)
            input.tooltip = "Search With Regular Expressions"
            input.tooltipDescription = (
                "Treat the search string as a Python regular expression (regex). "
                "This is extremely flexible but also very technical. Refer to "
                "Python documentation for details."
                "<p>One example is to put $ in the search box. This special "
                "symbol searches for the end of the setup name. Then the replacement "
                "string will be appended to the existing name."
            )

            # text box as a label for search field
            input = inputGroup.children.addTextBoxCommandInput("searchLabel", 
                                                               "", 
                                                               "Search for this string:",
                                                               1,
                                                               True)
            input.isFullWidth = True
            label = input

            # Find
            input = inputGroup.children.addStringValueInput("findString", "")
            input.isFullWidth = True
            input.tooltip = "String to find in setup name"
            input.tooltipDescription = (
                "Replace all occurences of this string with the replacement string. "
                "If this is left blank, the replacement string will be prepended to "
                "each setup name."
            )
            label.tooltip = input.tooltip
            label.tooltipDescription = input.tooltipDescription

            # text box as a label for replace field
            input = inputGroup.children.addTextBoxCommandInput("replaceLabel", 
                                                               "", 
                                                               "Replace with this string:",
                                                               1,
                                                               True)
            input.isFullWidth = True
            label = input

            # Replace
            input = inputGroup.children.addStringValueInput("replaceString", "")
            input.isFullWidth = True
            input.tooltip = "String to use as replacement"
            input.tooltipDescription = (
                "Replace all occurences of the Find string with this string."
            )
            label.tooltip = input.tooltip
            label.tooltipDescription = input.tooltipDescription

            # button to execute search & replace
            input = inputGroup.children.addBoolValueInput("replace", "Search and replace", False)
            input.resourceFolder = "resources/Rename"
            input.tooltip = "Execute search and replace"
            input.tooltipDescription = (
                "Search for all strings matching the Find box and replace them "
                "with the string in the Replace box.")
            inputGroup.isExpanded = docSettings["groupRename"]

            # Advanced -- retry settings
            inputGroup = inputs.addGroupCommandInput("groupAdvanced", "Advanced")
            # Time delay
            input = inputGroup.children.addFloatSpinnerCommandInput("initialDelay", 
                "Initial time allowance", "s", 0.1, 1.0, 0.1, docSettings["initialDelay"])
            input.tooltip = "Initial Time to Post Process an Operation"
            input.tooltipDescription = (
                "Initial delay to wait for post processor. Doubled for each retry.")
            # Retry count
            input = inputGroup.children.addIntegerSpinnerCommandInput("postRetries", 
                "Number of retries", 1, 9, 1, docSettings["postRetries"])
            input.tooltip = "Number of Retries"
            input.tooltipDescription = (
                "Retries if post processing failed. Time delay is doubled each retry.")
            inputGroup.isExpanded = docSettings["groupAdvanced"]
            
            # post processor
            inputGroup = inputs.addGroupCommandInput("groupPost", "Post Processor")
            inputGroup.isExpanded = docSettings["groupPost"]

            # Numeric name required?
            input = inputGroup.children.addBoolValueInput("numericName",
                                                          "Name must be numeric",
                                                          True,
                                                          "",
                                                          docSettings["numericName"])
            input.tooltip = "Output File Name Must Be Numeric"
            input.tooltipDescription = (
                "The name of the setup will not be used in the file name, "
                "only sequence numbers. The option to prepend sequence numbers "
                "will have no effect.")

            # button to save default settings
            input = inputs.addBoolValueInput("save", "Save as default", False)
            input.resourceFolder = "resources/Save"
            input.tooltip = "Save These Settings as System Default"
            input.tooltipDescription = (
                "Save these settings to use as the default for each new design.")

            # text box for error messages
            input = inputs.addTextBoxCommandInput("error", "", "", 3, True)
            input.isFullWidth = True
            input.isVisible = False

            # Connect to the inputChanged event.
            onInputChanged = CommandInputChangedHandler(docSettings, selectedSetups)
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
    def __init__(self, docSettings, selectedSetups):
        self.docSettings = docSettings
        self.selectedSetups = selectedSetups
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

            elif input.id == "replace":
                cmd.doExecute(False)    # do it in execute handler for Undo
                return

            elif input.id in self.docSettings:
                if input.objectType == adsk.core.GroupCommandInput.classType():
                    self.docSettings[input.id] = input.isExpanded
                elif input.objectType == adsk.core.DropDownCommandInput.classType():
                    self.docSettings[input.id] = input.selectedItem.name
                else:
                    self.docSettings[input.id] = input.value

            # Enable twoDigits only if sequence is true
            if input.id == "sequence":
                inputs.itemById("twoDigits").isEnabled = input.value

            # Enable delFolder only if delFiles is true
            if input.id == "delFiles":
                item = inputs.itemById("delFolder")
                item.value = input.value and item.value
                item.isEnabled = input.value

            # Update setup-dependent options when onlySelected changes
            if input.id == "onlySelected":
                hasTurning, hasMilling = GetSetupTypes(cam, self.selectedSetups, input.value)
                fastZItem = inputs.itemById("fastZ")
                if fastZItem:
                    fastZItem.isEnabled = hasTurning
                fastZHeuristicItem = inputs.itemById("fastZHeuristic")
                if fastZHeuristicItem:
                    splitVal = inputs.itemById("splitSetup").value if inputs.itemById("splitSetup") else False
                    fastZHeuristicItem.isEnabled = hasMilling and splitVal
                verifyItem = inputs.itemById("verifyGcode")
                if verifyItem:
                    verifyItem.isEnabled = hasTurning

            # Options for splitSetup
            if input.id == "splitSetup":
                inputs.itemById("combineTool").isEnabled = input.value
                inputs.itemById("toolChange").isEnabled = input.value
                inputs.itemById("toolLabel").isEnabled = input.value
                inputs.itemById("endCodes").isEnabled = input.value
                inputs.itemById("endLabel").isEnabled = input.value
                onlySelVal = inputs.itemById("onlySelected").value if inputs.itemById("onlySelected") else False
                _, hasMilling = GetSetupTypes(cam, self.selectedSetups, onlySelVal)
                fastZHeuristicItem = inputs.itemById("fastZHeuristic")
                if fastZHeuristicItem:
                    fastZHeuristicItem.isEnabled = hasMilling and input.value

            if input.id == "fastZ" and input.value:
                verifyItem = inputs.itemById("verifyGcode")
                if verifyItem:
                    verifyItem.value = True
                    self.docSettings["verifyGcode"] = True

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
    def __init__(self, docSettings, selectedSetups):
        self.docSettings = docSettings
        self.selectedSetups = selectedSetups
        super().__init__()

    def notify(self, args):
        eventArgs = adsk.core.CommandEventArgs.cast(args)
        cmd = eventArgs.command
        inputs = cmd.commandInputs

        # Code to react to the event.
        button = inputs.itemById("replace")
        if button.value:
            RenameSetups(self.docSettings, 
                        self.selectedSetups, 
                        inputs.itemById("findString").value, 
                        inputs.itemById("replaceString").value,
                        inputs.itemById("regex").value)
            button.value = False
        else:
            PerformPostProcess(self.docSettings, self.selectedSetups)


def PerformPostProcess(docSettings, setups):
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

        program = GetNcProgram(cam, docSettings);
        parameters = program.parameters
        setups = GetSetups(cam, docSettings, setups)

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
            # make sure we're not going to delete too much
            if not docSettings["delFiles"]:
                docSettings["delFolder"] = False

            if docSettings["delFolder"]:
                fileExt = parameters.itemByName("nc_program_nc_extension").value.value
                strMsg = CountOutputFolderFiles(outputFolder, len(setups), fileExt)
                if strMsg:
                    docSettings["delFolder"] = False
                    strMsg = (
                        "The output folder contains {}. "
                        "It will not be deleted. You may wish to make sure you selected "
                        "the correct folder. If you want the folder deleted, you must "
                        "do it manually."
                        ).format(strMsg)
                    res = ui.messageBox(strMsg, 
                                        constCmdName,
                                        adsk.core.MessageBoxButtonTypes.OKCancelButtonType,
                                        adsk.core.MessageBoxIconTypes.WarningIconType)
                    if res == adsk.core.DialogResults.DialogCancel:
                        return  # abort!

            if docSettings["delFolder"]:
                try:
                    shutil.rmtree(outputFolder, True)
                except:
                    pass #ignore errors

            progress = ui.createProgressDialog()
            progress.isCancelButtonShown = True
            progressMsg = "{} files written to " + outputFolder
            progress.show("Post Processing...", "", 0, len(setups))
            progress.progressValue = 1 # try to get it to display
            progress.progressValue = 0

            cntSetups = 0
            seqDict = dict()

            # We pass through all setups even if only some are selected
            # so numbering scheme doesn't change.
            for setup in cam.setups:
                if progress.wasCancelled:
                    break
                if not setup.isSuppressed and setup.allOperations.count != 0:
                    nameList = setup.name.split(':')    # folder separator
                    setupFolder = outputFolder
                    cnt = len(nameList) - 1
                    i = 0
                    while i < cnt:
                        setupFolder += "/" + nameList[i].strip()
                        i += 1
                
                    # keep a separate sequence number for each folder
                    if setupFolder in seqDict:
                        seqDict[setupFolder] += 1
                        # skip if we're not actually including this setup
                        if setup not in setups:
                            continue
                    else:
                        # first file for this folder
                        seqDict[setupFolder] = 1
                        # skip if we're not actually including this setup
                        if setup not in setups:
                            continue

                        if (docSettings["delFiles"]):
                            # delete all the files in the folder
                            try:
                                for entry in os.scandir(setupFolder):
                                    if entry.is_file():
                                        try:
                                            os.remove(entry.path)
                                        except:
                                            pass #ignore errors
                            except:
                                pass #ignore errors

                    # prepend sequence number if enabled
                    fname = nameList[i].strip()
                    if docSettings["sequence"] or docSettings["numericName"]:
                        seq = seqDict[setupFolder]
                        seqStr = str(seq)
                        if docSettings["twoDigits"] and seq < 10:
                            seqStr = "0" + seqStr
                        if docSettings["numericName"]:
                            fname = seqStr
                        else:
                            fname = seqStr + ' ' + fname

                    # post the file
                    status = PostProcessSetup(fname, setup, setupFolder, docSettings, program)
                    if status == None:
                        cntFiles += 1
                    else:
                        cntSkipped += 1
                        lstSkipped += "\nFailed on setup " + setup.name + ": " + status
                        
                cntSetups += 1
                progress.message = progressMsg.format(cntFiles)
                progress.progressValue = cntSetups

            progress.hide()
            # restore program output folder
            AssignOutputFolder(parameters, outputFolder)

        # done with setups, report results
        if cntSkipped != 0:
            ui.messageBox("{} files were written. {} Setups were skipped due to error:{}".format(cntFiles, cntSkipped, lstSkipped), 
                constCmdName, 
                adsk.core.MessageBoxButtonTypes.OKButtonType,
                adsk.core.MessageBoxIconTypes.WarningIconType)
            
        elif cntFiles == 0:
            ui.messageBox('No CAM operations posted', 
                constCmdName, 
                adsk.core.MessageBoxButtonTypes.OKButtonType,
                adsk.core.MessageBoxIconTypes.WarningIconType)

        ShowVerifyResults(ui)
            

    except:
        if progress:
            progress.hide()
        if ui:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))

# Per-run list of lathe verification summaries, shown in a modal dialog at the end
verifyResults = []


def _RecordVerifyResult(setup, cncPath, reportText, reportPath=None, pngPath=None, promoted=None, spikes=None, volume_mm3=None, collisions=None, error=None):
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
    })
    try:
        adsk.core.Application.get().log(f"[PostProcessAll] {setup.name}:\n{reportText}")
    except Exception:
        pass


def _ExtractWorkpieceFromSetup(setup):
    """Extract axisymmetric stock and chuck geometry directly from native Fusion 360 Setup."""
    try:
        if not hasattr(setup, "parameters"):
            return None
        params = setup.parameters
        p_dia = params.itemByName("job_stockDiameter")
        p_len = params.itemByName("job_stockLength")
        if not p_dia or not p_len:
            return None
        # Fusion API parameters for lengths are in internal units (centimeters)
        d_outer_mm = float(p_dia.value.value) * 10.0
        length_mm = float(p_len.value.value) * 10.0

        p_inner = params.itemByName("job_stockDiameterInner")
        d_inner_mm = float(p_inner.value.value) * 10.0 if p_inner else 0.0

        p_front = params.itemByName("job_stockOffsetFront")
        z_max_mm = float(p_front.value.value) * 10.0 if p_front else 0.0
        z_min_mm = z_max_mm - length_mm

        fixtures = []
        p_clamp = params.itemByName("job_stockClampingLength")
        if p_clamp:
            clamp_len_mm = float(p_clamp.value.value) * 10.0
            if clamp_len_mm > 0.1:
                from geometry import AxisymmetricChuckObstacle
                fixtures.append(
                    AxisymmetricChuckObstacle(
                        z_min=z_min_mm - 50.0,
                        z_max=z_min_mm + clamp_len_mm,
                        outer_radius=(d_outer_mm * 0.5) + 30.0,
                        clamping_radius=d_outer_mm * 0.5,
                    )
                )

        from geometry import AxisymmetricStock2D, WorkpieceDefinition
        return WorkpieceDefinition(
            stock=AxisymmetricStock2D(
                z_min=z_min_mm,
                z_max=z_max_mm,
                outer_radius=d_outer_mm * 0.5,
                inner_radius=d_inner_mm * 0.5,
            ),
            fixtures=fixtures,
        )
    except Exception:
        return None


def _ExtractToolsFromSetup(setup):
    """
    Analytically extract lathe tool parameters from native Fusion 360 Setup operations.
    Returns Dict[str, dict] with normalized metric dimensions for every operation.
    """
    tools_data = {}
    try:
        ops = []
        if hasattr(setup, "allOperations"):
            ops = list(setup.allOperations)
        elif hasattr(setup, "operations"):
            ops = list(setup.operations)
        elif isinstance(setup, (list, tuple)):
            ops = list(setup)

        for op in ops:
            op_name = getattr(op, "name", "")
            if not op_name:
                continue

            tool = getattr(op, "tool", None)
            tool_params = getattr(tool, "parameters", None) if tool else None
            op_params = getattr(op, "parameters", None)

            def _get_num(names, default, scale=10.0):
                for p_col in (tool_params, op_params):
                    if not p_col:
                        continue
                    for n in names:
                        try:
                            p = p_col.itemByName(n)
                            if p is not None:
                                val = p.value.value if hasattr(p, "value") and hasattr(p.value, "value") else p
                                if isinstance(val, (int, float)):
                                    return float(val) * scale
                                elif isinstance(val, str):
                                    s = val.strip().lower()
                                    if s.endswith("in"):
                                        return float(s[:-2].strip()) * 25.4
                                    elif s.endswith("mm"):
                                        return float(s[:-2].strip())
                                    return float(s) * scale
                        except Exception:
                            pass
                return default

            def _get_str(names, default=""):
                for p_col in (tool_params, op_params):
                    if not p_col:
                        continue
                    for n in names:
                        try:
                            p = p_col.itemByName(n)
                            if p is not None:
                                val = p.value.value if hasattr(p, "value") and hasattr(p.value, "value") else p
                                return str(val)
                        except Exception:
                            pass
                return default

            tool_id = int(_get_num(["tool_number", "number"], default=0, scale=1.0))
            ttype = _get_str(["tool_type", "type"], default="turning general").lower()
            is_boring = "bore" in ttype or "boring" in ttype or "internal" in op_name.lower() or "bore" in op_name.lower()
            is_drill = "drill" in ttype
            is_groove = "groov" in ttype or "part" in ttype

            hand = _get_str(["tool_hand", "hand"], default="R").upper()
            hand_code = "L" if ("LEFT" in hand or hand.startswith("L")) else "R"

            # Dimensions: Fusion internal unit cm -> mm (* 10.0)
            edge_l = _get_num(["tool_edgeLength", "edgeLength", "tool_insertEdgeLength"], default=9.525, scale=10.0)
            nose_r = _get_num(["tool_cornerRadius", "tool_noseRadius", "cornerRadius"], default=0.4, scale=10.0)
            lead_a = _get_num(["tool_leadAngle", "tool_leadingAngle", "leadAngle"], default=5.0, scale=180.0 / math.pi)
            shank_w = _get_num(["tool_shankWidth", "shankWidth"], default=16.0, scale=10.0)
            shank_l = _get_num(["tool_overallLength", "overallLength"], default=100.0, scale=10.0)
            dia = _get_num(["tool_diameter", "diameter"], default=6.35, scale=10.0)
            flute_l = _get_num(["tool_fluteLength", "fluteLength"], default=dia * 4.0, scale=10.0)
            blade_w = _get_num(["tool_width", "blade_width", "tool_diameter"], default=3.0, scale=10.0)
            blade_d = _get_num(["tool_bodyLength", "blade_depth", "tool_grooveDepth"], default=12.0, scale=10.0)

            shape = "drill" if is_drill else ("groove" if is_groove else "rhombic")

            tools_data[op_name] = {
                "tool_id": tool_id,
                "name": op_name,
                "insert_shape": shape,
                "is_boring": is_boring,
                "hand": hand_code,
                "edge_length": edge_l,
                "nose_radius": nose_r,
                "lead_angle": lead_a,
                "shank_width": shank_w,
                "shank_length": shank_l,
                "diameter": dia,
                "flute_length": flute_l,
                "overall_length": shank_l,
                "blade_width": blade_w,
                "blade_depth": blade_d,
            }
    except Exception:
        pass
    return tools_data


def _BuildToolAssembly(t_info):
    """Reconstruct pure-Python ToolAssembly with exact 2D Shapely geometry."""
    from shapely.geometry import Polygon
    from geometry.tool_generator import (
        generate_rhombic_insert,
        generate_grooving_insert,
        generate_drill_cutter,
        generate_turning_holder,
    )
    from geometry import LatheToolGeometry, ToolCompensationQuadrant, ToolAssembly

    shape = t_info.get("insert_shape", "rhombic")
    tool_id = t_info.get("tool_id", 0)
    op_name = t_info.get("name", "")
    is_boring = t_info.get("is_boring", False)
    shank_w = t_info.get("shank_width", 16.0)
    shank_l = t_info.get("shank_length", 100.0)
    nose_r = t_info.get("nose_radius", 0.4)

    if shape == "drill":
        cutter = generate_drill_cutter(
            diameter=t_info.get("diameter", 6.35),
            point_angle_deg=118.0,
            flute_length=t_info.get("flute_length", 30.0),
            overall_length=t_info.get("overall_length", 60.0),
        )
        holder = Polygon()
    elif shape == "groove":
        cutter = generate_grooving_insert(
            blade_width=t_info.get("blade_width", 3.0),
            blade_depth=t_info.get("blade_depth", 12.0),
            corner_radius=nose_r,
        )
        holder = generate_turning_holder(
            shank_width=shank_w,
            shank_length=shank_l,
            clearance_x=nose_r,
            clearance_z=nose_r,
            is_boring=is_boring,
        )
    else:
        cutter = generate_rhombic_insert(
            edge_length=t_info.get("edge_length", 9.525),
            nose_radius=nose_r,
            lead_angle_deg=t_info.get("lead_angle", 5.0),
        )
        clr_x = max(1.25, nose_r * 3.0) if is_boring else max(0.4, nose_r * 1.5)
        clr_z = max(1.25, nose_r * 3.0) if is_boring else max(0.4, nose_r * 1.5)
        holder = generate_turning_holder(
            shank_width=shank_w,
            shank_length=shank_l,
            clearance_x=clr_x,
            clearance_z=clr_z,
            is_boring=is_boring,
        )


    clean_holder = holder.difference(cutter.buffer(0.001)) if not holder.is_empty else holder
    geom = LatheToolGeometry(
        insert_shape=shape,
        nose_radius=nose_r,
        hand=t_info.get("hand", "R"),
        quadrant=ToolCompensationQuadrant.P3,
        cutter_polygon=cutter,
        holder_polygon=clean_holder,
    )
    return ToolAssembly(tool_id=tool_id, name=op_name, geometry=geom)


def RunLatheVerifier(cncPath, setup, docSettings, base_post=None):
    """
    Execute 6-stage toolpath verification and rapid-promotion pipeline:
    1. Ingest WorkpieceDefinition and ToolAssembly models analytically from Setup.
    2. Extract ISO 4343 APT toolpath and Compact-NCI (.cnc) via apt_extractor_wrapper.cps.
    3. Simulate 2D IPW stock removal and verify collisions via Lathe2DIPWVerifier.
    4. If fastZ is enabled, restore native G0 rapid moves via universal_wrapper.cps.
    5. Compile markdown verification report and generate high-res verification plot (.png).
    """
    if setup is None or setup.operationType != adsk.cam.OperationTypes.TurningOperation:
        return None
    if not (docSettings.get("verifyGcode", True) or docSettings.get("fastZ", False)):
        return None
    if not os.path.isfile(cncPath):
        return None

    try:
        with open(cncPath, "r", encoding="utf8", errors="replace") as f:
            gcode_content = f.read()

        promote = bool(docSettings.get("fastZ", False))
        basePath = os.path.splitext(cncPath)[0]
        reportPath = basePath + "_report.md"
        plotPath = basePath + ".png"

        # Ensure add-on root and packages are in sys.path
        import sys
        if constAddonDir not in sys.path:
            sys.path.insert(0, constAddonDir)
        if sys.version_info >= (3, 14) and os.path.isdir(constPackagesDir) and constPackagesDir not in sys.path:
            sys.path.insert(0, constPackagesDir)

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

        # Ingest workpiece and tools directly from native Fusion Setup API
        workpiece = _ExtractWorkpieceFromSetup(setup)
        tools_data = _ExtractToolsFromSetup(setup)
        tools_by_op = {}

        is_inch = ("G20" in gcode_content) or not ("G21" in gcode_content)
        tool_scale = 1.0

        if not workpiece or not tools_data:
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
                elif not tools_data and line.startswith(";META_OP:"):
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
            if not tools_data and is_inch:
                tool_scale = 25.4

        if not workpiece:
            return None

        # Locate intermediate Compact-NCI (.cnc)
        intermediate_path = cncPath + ".intermediate.cnc"
        if not os.path.isfile(intermediate_path):
            inter_dir = "/Users/vadimf/Documents/NCs/intermediates"
            if os.path.isdir(inter_dir):
                candidates = sorted(
                    glob.glob(os.path.join(inter_dir, "*.cnc")),

                    key=os.path.getmtime,
                    reverse=True,
                )
                if candidates:
                    intermediate_path = candidates[0]

        if not os.path.isfile(intermediate_path):
            try:
                adsk.core.Application.get().log(
                    f"[PostProcessAll] Intermediate toolpath not found for {cncPath}; skipping pipeline verification."
                )
            except Exception:
                pass
            return None

        # Base post processor configuration
        if not base_post or not os.path.isfile(base_post):
            base_post = os.path.join(constAddonDir, "centroid_turning.cps")
        if not os.path.isfile(base_post):
            return f"Base machine post processor not found: {base_post}"

        if can_import_in_process:
            if tools_data:
                tools_by_op = {name: _BuildToolAssembly(t_info) for name, t_info in tools_data.items()}
            verifier = Lathe2DIPWVerifier(tool_scale_to_mm=tool_scale)
            orchestrator = PipelineOrchestrator(
                wrapper_cps=os.path.join(constAddonDir, "universal_wrapper.cps")
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
            )

            fatal_cols = [
                c for c in report.collisions
                if c.kind in (CollisionKind.RAPID_INTO_STOCK, CollisionKind.HOLDER_STRIKE, CollisionKind.FIXTURE_COLLISION)
            ]
            err = f"{len(fatal_cols)} fatal collision(s) detected!" if fatal_cols else None
            promoted_cnt = len(report.promoted_vectors) if promote else 0
            spikes_cnt = len(report.mrr_spikes)
            vol_mm3 = report.total_volume_removed_mm3
            cols_list = report.collisions
        else:
            # Subprocess execution via isolated venv Python (bypasses macOS hardened runtime Team ID restriction)
            venvCandidates = [
                os.path.join(constAddonDir, ".venv", "bin", "python"),
                os.path.join(constAddonDir, ".venv", "bin", "python3"),
                os.path.expanduser("~/.gemini/antigravity-cli/brain/a05954bb-1c8d-4776-80f8-88db191a980c/scratch/venv/bin/python"),
                os.path.expanduser("~/Documents/.lathe_venv/bin/python"),
            ]
            venvPy = next((p for p in venvCandidates if os.path.isfile(p)), None)
            if not venvPy:
                return "Lathe verifier dependencies (shapely) could not be loaded in Fusion process or isolated venv."

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
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=cleanEnv)
                if proc.returncode != 0:
                    err_line = (proc.stderr.strip().splitlines() or proc.stdout.strip().splitlines() or [f"exit code {proc.returncode}"])[-1]
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
                err = f"{collision_cnt} fatal collision(s) detected!" if collision_cnt > 0 else None
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
            error=err,
        )

        if err:
            return f"Lathe verification detected {err} in {os.path.basename(cncPath)}"
        return None

    except Exception as exc:
        return f"Lathe pipeline verifier failed on {os.path.basename(cncPath)}: {exc}"


def ShowVerifyResults(ui):
    if not verifyResults:
        return
    lines = []
    for r in verifyResults:
        status = "FAILED: " + r["error"] if r["error"] else "OK"
        lines.append(f"{r['setup']}: {status}")
        if r["promoted"] is not None:
            lines.append(f"    G0 Rapids Restored: {r['promoted']}")
        if r.get("volume_mm3") is not None:
            vol = r["volume_mm3"]
            vol_in3 = vol / (25.4 ** 3)
            lines.append(f"    Total Material Removed: {vol:.2f} mm³ ({vol_in3:.3f} in³)")
        if r["spikes"] is not None:
            lines.append(f"    MRR spikes flagged: {r['spikes']}")
        for kind, n in sorted(r["kinds"].items()):
            lines.append(f"    {kind}: {n}")
        lines.append(f"    G-code: {r['cnc']}")
        if r.get("png") and os.path.isfile(r["png"]):
            lines.append(f"    Plot:   {r['png']}")
        if r.get("report") and os.path.isfile(r["report"]):
            lines.append(f"    Report: {r['report']}")
        lines.append("")
    lines.append("Open the verification plot(s) and report(s) now?")
    res = ui.messageBox("\n".join(lines),
                        constCmdName + " - Lathe Verification",
                        adsk.core.MessageBoxButtonTypes.YesNoButtonType,
                        adsk.core.MessageBoxIconTypes.InformationIconType)
    if res == adsk.core.DialogResults.DialogYes:
        failures = []
        for r in verifyResults:
            for p in (r.get("png"), r.get("report")):
                if p and os.path.isfile(p):
                    err = _OpenFile(p)
                    if err:
                        failures.append(f"{p}: {err}")
        if failures:
            ui.messageBox("Could not open:\n" + "\n".join(failures), constCmdName,
                          adsk.core.MessageBoxButtonTypes.OKButtonType,
                          adsk.core.MessageBoxIconTypes.WarningIconType)


def _OpenFile(path):
    """Open a file in the OS viewer. Returns None on success or an error string."""
    try:
        if os.name != "posix":
            os.startfile(path)
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
                return None
            last = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
        return last
    except Exception as exc:
        return str(exc)


def PostProcessSetup(fname, setup, setupFolder, docSettings, program):
    ui = None
    fileHead = None
    fileBody = None
    fileOp = None
    retVal = "Fusion reported an exception"

    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface
        doc = app.activeDocument
        cam = adsk.cam.CAM.cast(doc.products.itemByProductType(constCAMProductId))
        parameters = program.parameters

        # Verify file name is valid by creating it now
        fileExt = parameters.itemByName("nc_program_nc_extension").value.value
        path = setupFolder + "/" + fname + fileExt
        try:
            pathlib.Path(setupFolder).mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf8"):
                pass
            if docSettings["splitSetup"]:
                os.remove(path)
        except Exception as exc:
            return "Unable to create output file '" + path + "'. Make sure the setup name is valid as a file name."
        
        # Make sure toolpaths are valid
        if not cam.checkToolpath(setup):
            genStat = cam.generateToolpath(setup)
            while not genStat.isGenerationCompleted:
                time.sleep(.1)

        # Post directly to setupFolder/fname so Fusion's toast notification links to the real output file
        opName = fname
        opFolder = setupFolder
        tmpFolder = tempfile.gettempdir().replace("\\", "/")

        parameters.itemByName("nc_program_openInEditor").value.value = False
        AssignOutputFolder(parameters, opFolder)
        parameters.itemByName("nc_program_filename").value.value = opName
        parameters.itemByName("nc_program_name").value.value = fname

        # Check if lathe verification pipeline should handle this setup
        is_turning = (setup.operationType == adsk.cam.OperationTypes.TurningOperation)
        do_lathe_pipeline = is_turning and (docSettings.get("verifyGcode", True) or docSettings.get("fastZ", False))

        # Do it all at once? (Always handle turning setups through unified verification pipeline)
        if do_lathe_pipeline or not docSettings["splitSetup"]:
            orig_post = None
            try:
                program.operations = [setup]
                if do_lathe_pipeline and os.path.isfile(constAptExtractorScript):
                    try:
                        orig_post = program.postConfiguration
                        intermediate_cnc = path + ".intermediate.cnc"
                        os.environ["SYSTEM_APT_CPS"] = "apt.cps"
                        os.environ["TARGET_INTERMEDIATE_CNC"] = intermediate_cnc
                        program.postConfiguration = constAptExtractorScript
                    except Exception:
                        orig_post = None

                if not program.postProcess(adsk.cam.NCProgramPostProcessOptions.create()):
                    return "Fusion reported an error."
                time.sleep(constPostLoopDelay) # files missing sometimes unless we slow down (??)
                if do_lathe_pipeline:
                    return RunLatheVerifier(path, setup, docSettings, base_post=orig_post)
                return None
            except Exception as exc:
                retVal += ": " + str(exc)
                return retVal
            finally:
                if orig_post:
                    try:
                        program.postConfiguration = orig_post
                    except Exception:
                        pass


        # Split setup into individual operations
        opPath = path
        fileHead = io.StringIO()
        fileBody = open(tmpFolder + "/" + constBodyTmpFile + fileExt, "w", encoding="utf8")
        fFirst = True
        fBlankOk = False
        lineNum = 10
        regToolComment = re.compile(r"\(T[0-9]+\s")
        regBody = re.compile(r""
            r"(?P<N>N[0-9]+ *)?" # line number
            r"(?P<line>"         # line w/o number
            r"(M(?P<M>[0-9]+) *)?" # M-code
            r"(G(?P<G>[0-9]+) *)?" # G-code
            r"(T(?P<T>[0-9]+))?" # Tool
            r".+)",              # to end of line
            re.IGNORECASE | re.DOTALL)
        toolChange = docSettings["toolChange"]
        fToolChangeNum = False
        if len(toolChange) != 0:
            toolChange = toolChange.replace(":", "\n")
            toolChange += "\n"
            match = regBody.match(toolChange).groupdict()
            if match["N"] != None:
                fToolChangeNum = True
                toolChange = match["line"]
                # split into individual lines to add line numbers
                toolChange = toolChange.splitlines(True)
        # Parse end code list, splitting into G-codes and M-codes
        endCodes = docSettings["endCodes"]
        endGcodes = re.findall("G([0-9]+)", endCodes)
        endGcodeSet = set()
        for code in endGcodes:
            endGcodeSet.add(int(code))
        endMcodes = re.findall("M([0-9]+)", endCodes)
        endMcodeSet = set()
        for code in endMcodes:
            endMcodeSet.add(int(code))

        fFastZenabled = bool(docSettings.get("fastZHeuristic", docSettings.get("fastZ", False)))
        if fFastZenabled:
            regParseLine = re.compile(r""
                r"(G(?P<G>[0-9]+(\.[0-9]*)?)[^XYZF]*)?"
                r"(?P<XY>((X-?[0-9]+(\.[0-9]*)?)[^XYZF]*)?"
                r"((Y-?[0-9]+(\.[0-9]*)?)[^XYZF]*)?)"
                r"(Z(?P<Z>-?[0-9]+(\.[0-9]*)?)[^XYZF]*)?"
                r"(F(?P<F>-?[0-9]+(\.[0-9]*)?)[^XYZF]*)?",
                re.IGNORECASE)
            regGcodes = re.compile(r"G([0-9]+(?:\.[0-9]*)?)")

        i = 0
        ops = setup.allOperations
        while i < ops.count:
            op = ops[i]
            i += 1
            if op.isSuppressed:
                continue

            # Look ahead for operations without a toolpath. This can happen
            # with a manual operation. Group it with current operation.
            # Or if first, group it with subsequent ones.
            # Also optionally group together operations with the same tool number
            opHasTool = None
            curTool = -1
            hasTool = op.hasToolpath
            if hasTool:
                opHasTool = op
                curTool = op.tool.parameters.itemByName("tool_number").value.value
            opList = [op]
            while i < ops.count:
                op = ops[i]
                if not op.isSuppressed:
                    if op.hasToolpath and (not docSettings["combineTool"] or op.tool.parameters.itemByName("tool_number").value.value != curTool):
                        if not hasTool:
                            opList.append(op)
                            opHasTool = op
                            i += 1
                        break
                    opList.append(op)
                i += 1

            retries = docSettings["postRetries"]
            delay = docSettings["initialDelay"]
            while True:
                try:
                    program.operations = opList
                    if not program.postProcess(adsk.cam.NCProgramPostProcessOptions.create()):
                        retVal = "Fusion reported an error processing operation"
                        if (opHasTool != None):
                            retVal += ": " +  opHasTool.name
                        return retVal
                except Exception as exc:
                    if (opHasTool != None):
                        retVal += " in operation " +  opHasTool.name
                    retVal += ": " + str(exc)
                    return retVal

                time.sleep(delay) # wait for it to finish (??)
                try:
                    fileOp = open(opPath, encoding="utf8", errors='replace')
                    break
                except:
                    delay *= 2
                    retries -= 1
                    if retries > 0:
                        continue
                    # Maybe the file name extension is wrong
                    for file in os.listdir(opFolder):
                        if file.startswith(opName):
                            ext = file[len(opName):]
                            if ext != fileExt:
                                return ("Unable to open output file. "
                                    "Found the file with extension '{}' instead "
                                    "of '{}'. Make sure you have the correct file "
                                    "extension set in the Post Process All "
                                    "dialog.".format(ext, fileExt))
                            break
                    return "Unable to open " + opPath
            
            # Space between operations
            if not fFirst and fBlankOk:
                fileBody.write("\n")

            # % at start only
            line = fileOp.readline()
            if line[0] == "%":
                if fFirst:
                    fileHead.write(line)
                line = fileOp.readline()

            # check for initial comments and tool
            # send it to header
            while line[0] == "(" or line[0] == "O" or line[0] == "\n":
                if line[0] == "\n":
                    fBlankOk = True
                if regToolComment.match(line) != None:
                    fileHead.write(line)
                    line = fileOp.readline()
                    break

                if fFirst:
                    pos = line.upper().find(opName.upper())
                    if pos != -1:
                        pos += len(opName)
                        if docSettings["numericName"]:
                            fill = "0" * (pos - len(fname) - 1)
                        else:
                            fill = ""
                        line = line[0] + fill + fname + line[pos:]    # correct file name
                    fileHead.write(line)
                line = fileOp.readline()

            # Body starts at tool code, T
            while True:
                match = regBody.match(line).groupdict()
                line = match["line"]        # filter off line number if present
                fNum = match["N"] != None

                if (match["T"] != None):
                    if not fFirst and len(toolChange) != 0:
                        # have tool change G-codes to add
                        if fToolChangeNum:
                            # Add line number to tool change
                            for code in toolChange:
                                fileBody.write("N" + str(lineNum) + " " + code)
                                lineNum += constLineNumInc
                        else:
                            fileBody.write(toolChange)
                    break

                if fFirst or line[0] == "(" or line[0] == ";":
                    if (fNum):
                        fileBody.write("N" + str(lineNum) + " ")
                        lineNum += constLineNumInc
                    fileBody.write(line)
                line = fileOp.readline()
                if len(line) == 0:
                    return "Tool change G-code (Txx) not found; this post processor is not compatible with Post Process All."
                if line[0] == "\n":
                    fBlankOk = True

            # Note that match, line, and fNum are already set
            # Initialize rapid move optimizations for milling operations
            fFastZ = fFastZenabled and (setup.operationType != adsk.cam.OperationTypes.TurningOperation)
            Gcode = None
            Zcur = None
            Zlast = None
            Zfeed = None
            feedCur = 0
            fNeedFeed = False
            fLockSpeed = False

            while True:
                # End of program marker?
                endMark = match["M"]
                if endMark != None:
                    endMark = int(endMark)
                    if endMark in endMcodeSet:
                        break
                    # When M49/M48 is used to turn off speed changes, disable fast moves as well
                    if endMark == 49:
                        fLockSpeed = True
                    elif endMark == 48:
                        fLockSpeed = False
                endMark = match["G"]
                if endMark != None:
                    endMark = int(endMark)
                    if endMark in endGcodeSet:
                        break

                if fFastZ:
                    # Analyze code for chances to make rapid moves
                    matchParsed = regParseLine.match(line)
                    if matchParsed is not None and matchParsed.end() != 0:
                        try:
                            matchDict = matchParsed.groupdict()
                            Gcodes = regGcodes.findall(line)
                            fNoMotionGcode = True
                            fHomeGcode = False
                            for GcodeTmp in Gcodes:
                                GcodeTmp = int(float(GcodeTmp))
                                if GcodeTmp in constHomeGcodeSet:
                                    fHomeGcode = True
                                    break

                                if GcodeTmp in constMotionGcodeSet:
                                    fNoMotionGcode = False
                                    Gcode = GcodeTmp
                                    if Gcode == 0:
                                        fNeedFeed = False
                                    break

                            if not fHomeGcode:
                                Ztmp = matchDict["Z"]
                                if Ztmp is not None:
                                    Zlast = Zcur
                                    Zcur = float(Ztmp)

                                feedTmp = matchDict["F"]
                                prevFeed = feedCur
                                if feedTmp is not None:
                                    feedCur = float(feedTmp)

                                XYcur = matchDict["XY"].rstrip("\n ")

                                # 1. Learn Zfeed on the first pure-Z move (G0 clearance or initial feed-height move)
                                if Zfeed is None and (Gcode == 0 or Gcode == 1) and Ztmp is not None and len(XYcur) == 0:
                                    Zfeed = Zcur
                                    if Gcode != 0:
                                        # Replace line with rapid move
                                        line = constRapidZgcode.format(Zcur, line[:-1])
                                        fNeedFeed = True
                                        Gcode = 0

                                # 2. Candidate rapid moves at or above feed height
                                if Gcode == 1 and not fLockSpeed:
                                    if Ztmp is not None:
                                        is_at_feed = (Zfeed is not None and Zcur >= Zfeed)
                                        is_upward_safe = (Zlast is not None and Zcur >= Zlast and Zfeed is not None and Zcur >= Zfeed)
                                        is_zero_feed = (feedCur == 0)
                                        if len(XYcur) == 0 and (is_at_feed or is_upward_safe or is_zero_feed):
                                            # Move at or above feed height, or anomalous feed rate -> promote to rapid
                                            line = constRapidZgcode.format(Zcur, line[:-1])
                                            fNeedFeed = True
                                            Gcode = 0

                                    elif Zcur is not None and Zfeed is not None and Zcur >= Zfeed:
                                        # No Z move, at/above feed height -> promote XY traverse to rapid
                                        line = constRapidXYgcode.format(XYcur, line[:-1])
                                        fNeedFeed = True
                                        Gcode = 0

                                # 3. Restoring G01 and modal feedrate when descending below feed height
                                elif fNeedFeed and fNoMotionGcode:
                                    # No explicit G-code present on line, changing back to G1
                                    if Ztmp is not None:
                                        if len(XYcur) != 0:
                                            # Combined XYZ cutting move -> restore G1 with feedrate
                                            line = constFeedXYZgcode.format(XYcur, Zcur, feedCur, line[:-1])
                                            fNeedFeed = False
                                            Gcode = 1
                                        elif Zfeed is not None and Zcur < Zfeed:
                                            # Pure Z move below feed height -> restore G1 with feedrate
                                            line = constFeedZgcode.format(Zcur, feedCur, line[:-1])
                                            fNeedFeed = False
                                            Gcode = 1

                                    elif len(XYcur) != 0 and Zcur is not None and Zfeed is not None and Zcur < Zfeed:
                                        # No Z move, below feed height -> restore G1 with feedrate
                                        line = constFeedXYgcode.format(XYcur, feedCur, line[:-1])
                                        fNeedFeed = False
                                        Gcode = 1

                                # 4. Re-add feedrate if motion was restored to cutting G1 without an explicit F word
                                if Gcode != 0 and fNeedFeed:
                                    if feedTmp is None:
                                        line = line[:-1] + constAddFeedGcode.format(feedCur)
                                    fNeedFeed = False

                                # 5. Adapt Zfeed upwards if a cutting move occurs at or above current Zfeed
                                if Zcur is not None and Zfeed is not None and Zcur >= Zfeed and Gcode is not None and \
                                    Gcode != 0 and len(XYcur) != 0 and (Ztmp is not None or Gcode != 1):
                                    Zfeed = Zcur + 0.001
                        except Exception:
                            fFastZ = False # Safely skip changes on parse error

                # copy line to output
                if (fNum):
                    fileBody.write("N" + str(lineNum) + " ")
                    lineNum += constLineNumInc
                fileBody.write(line)
                lineFull = fileOp.readline()
                if len(lineFull) == 0:
                    break
                match = regBody.match(lineFull).groupdict()
                line = match["line"]        # filter off line number if present
                fNum = match["N"] != None

            # Found tail of program
            if fFirst:
                tailGcode = lineFull + fileOp.read()
            fFirst = False
            fileOp.close()
            os.remove(fileOp.name)
            fileOp = None

        # Completed all operations, add tail
        # Update line numbers if present
        if len(tailGcode) != 0:
            tailGcode = tailGcode.splitlines(True)
            for code in tailGcode:
                match = regBody.match(code).groupdict()
                if match["N"] != None:
                    fileBody.write("N" + str(lineNum) + " " + match["line"])
                    lineNum += constLineNumInc
                else:
                    fileBody.write(code)

        # Write combined header + body to final output path
        fileBody.close()
        fileBody = open(fileBody.name, "r", encoding="utf8", errors="replace")
        with open(path, "w", encoding="utf8") as outFile:
            outFile.write(fileHead.getvalue())
            while True:
                block = fileBody.read(10240)
                if len(block) == 0:
                    break
                outFile.write(block)
        fileBody.close()
        os.remove(fileBody.name)
        fileBody = None
        fileHead.close()
        fileHead = None

        return RunLatheVerifier(path, setup, docSettings)

    except:
        if fileHead:
            try:
                fileHead.close()
            except:
                pass

        if fileBody:
            try:
                fileBody.close()
                os.remove(fileBody.name)
            except:
                pass

        if fileOp:
            try:
                fileOp.close()
                os.remove(fileOp.name)
            except:
                pass

        if ui:
            retVal += " " + traceback.format_exc()

        return retVal
