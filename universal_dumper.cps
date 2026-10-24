/**
 * Universal Context & Event Dumper (universal_dumper.cps)
 *
 * Pass 1 extractor for the G-code-dialect-independent verification & replay pipeline:
 * 1. Dynamically includes BASE_POST (if provided) so the C++ Post Kernel inherits the
 *    target vendor post's exact tolerance, chord length, and circular plane settings.
 * 2. Serializes all global metadata (units, machine configuration, workpiece bounding box,
 *    global parameters, comments) and per-section metadata (Tool, WCS, workPlane, Z-range,
 *    parameters, and ordered motion/state events) into structured JSON.
 * 3. Pre-captures C++ kernel-computed expansions during active records:
 *    - `linearizedMoves` inside `onCircular()` via `linearize(tolerance)`
 *    - `reposMoves`, `afterReposMoves`, and `expandedMoves` inside `onCyclePoint()`
 *      via `repositionToCycleClearance()` and `expandCyclePoint()`
 * 4. Optionally copies `getIntermediatePath()` to `TARGET_INTERMEDIATE_CNC` so the
 *    standalone Autodesk `post` binary has a bootstrap `.cnc` file for Pass 2 replay.
 */

description = "Universal Context & Event Dumper";
vendor = "Universal";
certificationLevel = 2;
minimumRevision = 45000;
capabilities = CAPABILITY_TURNING | CAPABILITY_MILLING | CAPABILITY_INTERMEDIATE;
extension = "json";

var _dumperCfg = null;
function _getDumperConfig() {
  if (_dumperCfg !== null) return _dumperCfg;
  _dumperCfg = {};
  var cfgCandidates = ["/tmp/universal_dumper_config.json"];
  try {
    var tf = FileSystem.getTemporaryFolder();
    if (tf) {
      cfgCandidates.unshift(FileSystem.getCombinedPath(tf, "universal_dumper_config.json"));
    }
  } catch (e) {}
  for (var i = 0; i < cfgCandidates.length; ++i) {
    try {
      if (FileSystem.isFile(cfgCandidates[i])) {
        _dumperCfg = JSON.parse(loadText(cfgCandidates[i], "utf-8"));
        break;
      }
    } catch (e) {}
  }
  return _dumperCfg;
}

var basePostPath = "";
try {
  basePostPath = getEnvironmentVariable("BASE_POST");
} catch (e) {}
if (!basePostPath) {
  try {
    var cfg = _getDumperConfig();
    if (cfg && cfg.basePost) {
      basePostPath = cfg.basePost;
    }
  } catch (e) {}
}
var vendorExtension = "nc";
if (basePostPath && FileSystem.isFile(basePostPath)) {
  try {
    include(basePostPath);
    if (typeof extension === "string" && extension && extension !== "json") {
      vendorExtension = extension;
    }
  } catch (e) {}
}

var _cfgProps = {};
try {
  var _dc = _getDumperConfig();
  if (_dc && _dc.postProperties && typeof _dc.postProperties === "object") {
    _cfgProps = _dc.postProperties;
    if (typeof properties === "object" && properties) {
      for (var _pk in _cfgProps) {
        if (Object.prototype.hasOwnProperty.call(properties, _pk)) {
          if (properties[_pk] !== null && typeof properties[_pk] === "object") {
            properties[_pk].value = _cfgProps[_pk];
            properties[_pk].current = _cfgProps[_pk];
          } else {
            properties[_pk] = _cfgProps[_pk];
          }
        }
      }
    }
  }
} catch (e) {}

// Re-assert universal capabilities and JSON extension after including BASE_POST
capabilities = CAPABILITY_TURNING | CAPABILITY_MILLING | CAPABILITY_INTERMEDIATE;
extension = "json";
description = "Universal Context & Event Dumper";
vendor = "Universal";

// Reset any output formatting filters from BASE_POST so raw JSON is written cleanly
try {
  setCodePage("utf-8");
} catch (e) {}

var dumpData = {
  unit: 0,
  highFeedrate: 0,
  programName: "",
  programComment: "",
  vendorExtension: vendorExtension,
  postProperties: _cfgProps,
  machine: {},
  workpiece: null,
  globalParams: {},
  comments: [],
  sections: []
};

var curSec = null;
var opened = false;
var pendingParams = {};
var activeCycleObj = null;
var capturingExpanded = null;

function serializeVector(v) {
  if (!v) return {x: 0, y: 0, z: 0};
  return {x: v.x, y: v.y, z: v.z};
}

function serializeMatrix(m) {
  if (!m) {
    return {
      right: {x: 1, y: 0, z: 0},
      up: {x: 0, y: 1, z: 0},
      forward: {x: 0, y: 0, z: 1}
    };
  }
  return {
    right: serializeVector(m.right),
    up: serializeVector(m.up),
    forward: serializeVector(m.forward)
  };
}

function serializeBox(b) {
  if (!b) return null;
  return {
    lower: serializeVector(b.lower),
    upper: serializeVector(b.upper)
  };
}

onMachine = function() {
  if (typeof machineConfiguration !== "undefined" && machineConfiguration) {
    dumpData.machine = {
      vendor: machineConfiguration.getVendor ? machineConfiguration.getVendor() : "",
      model: machineConfiguration.getModel ? machineConfiguration.getModel() : "",
      description: machineConfiguration.getDescription ? machineConfiguration.getDescription() : "",
      multiAxis: machineConfiguration.isMultiAxisConfiguration ? machineConfiguration.isMultiAxisConfiguration() : false,
      hasHomeX: machineConfiguration.hasHomePositionX ? machineConfiguration.hasHomePositionX() : false,
      homeX: machineConfiguration.getHomePositionX ? machineConfiguration.getHomePositionX() : 0,
      hasHomeY: machineConfiguration.hasHomePositionY ? machineConfiguration.hasHomePositionY() : false,
      homeY: machineConfiguration.getHomePositionY ? machineConfiguration.getHomePositionY() : 0,
      hasHomeZ: machineConfiguration.hasHomePositionZ ? machineConfiguration.hasHomePositionZ() : false,
      homeZ: machineConfiguration.getHomePositionZ ? machineConfiguration.getHomePositionZ() : 0
    };
  }
};

onOpen = function() {
  opened = true;
  dumpData.unit = unit;
  dumpData.highFeedrate = (typeof highFeedrate === "number") ? highFeedrate : 0;
  dumpData.programName = programName;
  dumpData.programComment = programComment;
  try {
    dumpData.workpiece = serializeBox(getWorkpiece());
  } catch (e) {}
};

onComment = function(c) {
  if (!curSec) {
    if (dumpData.sections.length === 0) {
      dumpData.comments.push(c);
    } else {
      dumpData.sections[dumpData.sections.length - 1].postComments.push(c);
    }
  } else {
    curSec.events.push({type: "onComment", c: c});
  }
};

onPassThrough = function(text) {
  if (curSec) {
    curSec.events.push({type: "onPassThrough", text: text});
  }
};

onParameter = function(name, value) {
  if (!opened) {
    dumpData.globalParams[name] = value;
  } else if (!curSec) {
    pendingParams[name] = value;
  } else {
    curSec.params[name] = value;
    curSec.events.push({type: "onParameter", name: name, value: value});
  }
};

onSection = function() {
  var t = {};
  for (var k in tool) {
    if (typeof tool[k] !== "function" && k !== "shaft" && k !== "holder") {
      t[k] = tool[k];
    }
  }
  t._manualToolChange = tool.getManualToolChange ? tool.getManualToolChange() : !!tool.manualToolChange;
  t._spindleMode = tool.getSpindleMode ? tool.getSpindleMode() : 0;
  t._taperAngle = tool.getTaperAngle ? tool.getTaperAngle() : (tool.taperAngle || 0);
  t._threadPitch = tool.getThreadPitch ? tool.getThreadPitch() : (tool.threadPitch || 0);
  t._tappingFeedrate = tool.getTappingFeedrate ? tool.getTappingFeedrate() : (tool.tappingFeedrate || 0);
  t._isTurningTool = tool.isTurningTool ? tool.isTurningTool() : false;
  t._isLiveTool = tool.isLiveTool ? tool.isLiveTool() : !!tool.liveTool;
  t._toolId = tool.getToolId ? tool.getToolId() : String(tool.number || "");
  t._description = tool.getDescription ? tool.getDescription() : (tool.description || "");
  t._comment = tool.getComment ? tool.getComment() : (tool.comment || "");
  t._extent = tool.getExtent ? serializeBox(tool.getExtent(false)) : null;
  t._cutterSVG = tool.getCutterProfileAsSVGPath ? tool.getCutterProfileAsSVGPath() : "";
  t._holderSVG = tool.getHolderProfileAsSVGPath ? tool.getHolderProfileAsSVGPath() : "";
  t._compDisplacement = tool.getCompensationDisplacement ? serializeVector(tool.getCompensationDisplacement()) : {x: 0, y: 0, z: 0};
  t._toolTypeName = (typeof getToolTypeName === "function" && tool.type !== undefined) ? getToolTypeName(tool.type) : "";

  var zRange = currentSection.getGlobalZRange ? currentSection.getGlobalZRange() : null;
  var isMulti = currentSection.isMultiAxis ? currentSection.isMultiAxis() : false;

  curSec = {
    id: currentSection.getId(),
    type: currentSection.getType(),
    quality: currentSection.quality || 0,
    jetMode: currentSection.jetMode || 0,
    workOffset: currentSection.workOffset,
    wcs: currentSection.wcs,
    probeWorkOffset: currentSection.probeWorkOffset,
    workOrigin: serializeVector(currentSection.workOrigin),
    workPlane: serializeMatrix(currentSection.workPlane),
    spindle: currentSection.spindle,
    feedMode: currentSection.feedMode,
    toolOrientation: currentSection.toolOrientation || 0,
    partCatcher: currentSection.partCatcher,
    tailstock: currentSection.tailstock,
    strategy: currentSection.strategy,
    initialPosition: serializeVector(currentSection.getInitialPosition()),
    globalInitialPosition: serializeVector(
      currentSection.getGlobalInitialPosition ? currentSection.getGlobalInitialPosition() : currentSection.getInitialPosition()
    ),
    finalPosition: serializeVector(currentSection.getFinalPosition()),
    globalFinalPosition: serializeVector(
      currentSection.getGlobalFinalPosition ? currentSection.getGlobalFinalPosition() : currentSection.getFinalPosition()
    ),
    initialToolAxis: currentSection.getInitialToolAxis ? serializeVector(currentSection.getInitialToolAxis()) : {x: 0, y: 0, z: 1},
    globalInitialToolAxis: currentSection.getGlobalInitialToolAxis ? serializeVector(currentSection.getGlobalInitialToolAxis()) : {x: 0, y: 0, z: 1},
    finalToolAxis: currentSection.getFinalToolAxis ? serializeVector(currentSection.getFinalToolAxis()) : {x: 0, y: 0, z: 1},
    globalFinalToolAxis: currentSection.getGlobalFinalToolAxis ? serializeVector(currentSection.getGlobalFinalToolAxis()) : {x: 0, y: 0, z: 1},
    initialToolAxisABC: (isMulti && currentSection.getInitialToolAxisABC) ? serializeVector(currentSection.getInitialToolAxisABC()) : {x: 0, y: 0, z: 0},
    finalToolAxisABC: (isMulti && currentSection.getFinalToolAxisABC) ? serializeVector(currentSection.getFinalToolAxisABC()) : {x: 0, y: 0, z: 0},
    forceToolChange: currentSection.getForceToolChange ? currentSection.getForceToolChange() : false,
    movements: currentSection.getMovements ? currentSection.getMovements() : 0,
    patternId: currentSection.getPatternId ? currentSection.getPatternId() : 0,
    internalPatternId: currentSection.getInternalPatternId ? currentSection.getInternalPatternId() : 0,
    isPatterned: currentSection.isPatterned ? currentSection.isPatterned() : false,
    hasAnyCycle: currentSection.hasAnyCycle ? currentSection.hasAnyCycle() : false,
    isOptional: currentSection.isOptional ? currentSection.isOptional() : false,
    isMultiAxis: isMulti,
    isOptimizedForMachine: currentSection.isOptimizedForMachine ? currentSection.isOptimizedForMachine() : false,
    optimizedTCPMode: currentSection.getOptimizedTCPMode ? currentSection.getOptimizedTCPMode() : 0,
    zRangeMin: zRange ? zRange.getMinimum() : 0,
    zRangeMax: zRange ? zRange.getMaximum() : 0,
    initSpindleSpeed: spindleSpeed,
    initMovement: movement,
    initRadiusComp: radiusCompensation,
    params: pendingParams,
    tool: t,
    events: [],
    postComments: []
  };
  pendingParams = {};
  dumpData.sections.push(curSec);
};

onSpindleSpeed = function(s) {
  curSec.events.push({type: "onSpindleSpeed", s: s});
};

onOrientateSpindle = function(angle) {
  curSec.events.push({type: "onOrientateSpindle", angle: angle});
};

onMovement = function(m) {
  if (capturingExpanded) return;
  curSec.events.push({type: "onMovement", m: m});
};

onFeedMode = function(fm) {
  curSec.events.push({type: "onFeedMode", fm: fm});
};

onRadiusCompensation = function() {
  curSec.events.push({type: "onRadiusCompensation", rc: radiusCompensation});
};

onDwell = function(sec) {
  if (capturingExpanded) {
    capturingExpanded.push({type: "onDwell", sec: sec});
    return;
  }
  curSec.events.push({type: "onDwell", sec: sec});
};

onCommand = function(cmd) {
  curSec.events.push({type: "onCommand", cmd: cmd});
};

onRapid = function(x, y, z) {
  var start = getCurrentPosition();
  var obj = {
    type: "onRapid", x: x, y: y, z: z,
    sx: start.x, sy: start.y, sz: start.z,
    movement: movement, rc: radiusCompensation, rpm: spindleSpeed
  };
  if (capturingExpanded) {
    capturingExpanded.push(obj);
    return;
  }
  curSec.events.push(obj);
};

onLinear = function(x, y, z, feed) {
  var start = getCurrentPosition();
  var obj = {
    type: "onLinear", x: x, y: y, z: z, feed: feed,
    sx: start.x, sy: start.y, sz: start.z,
    movement: movement, rc: radiusCompensation, rpm: spindleSpeed,
    sync: (typeof isSpeedFeedSynchronizationActive === "function") ? isSpeedFeedSynchronizationActive() : false
  };
  if (capturingExpanded) {
    capturingExpanded.push(obj);
    return;
  }
  curSec.events.push(obj);
};

onCircular = function(cw, cx, cy, cz, x, y, z, feed) {
  var start = getCurrentPosition();
  var n = getCircularNormal();
  var plane = getCircularPlane();
  var radius = getCircularRadius();
  var sweep = getCircularSweep();
  var fullCircle = isFullCircle();
  var helical = isHelical();
  var spiral = isSpiral();
  var curMov = movement;
  var curRc = radiusCompensation;
  var curRpm = spindleSpeed;

  var linMoves = [];
  capturingExpanded = linMoves;
  try {
    var tol = (typeof tolerance === "number" && tolerance > 0) ? tolerance : spatial(0.002, MM);
    linearize(tol);
  } catch (e) {}
  capturingExpanded = null;
  setCurrentPosition(new Vector(x, y, z));

  curSec.events.push({
    type: "onCircular", cw: cw, cx: cx, cy: cy, cz: cz, x: x, y: y, z: z, feed: feed,
    sx: start.x, sy: start.y, sz: start.z,
    plane: plane,
    radius: radius,
    sweep: sweep,
    nx: n.x, ny: n.y, nz: n.z,
    fullCircle: fullCircle,
    helical: helical,
    spiral: spiral,
    movement: curMov, rc: curRc, rpm: curRpm,
    linearizedMoves: linMoves
  });
};

onCycle = function() {
  var c = {};
  for (var k in cycle) {
    if (typeof cycle[k] !== "function") {
      var val = cycle[k];
      if (val && typeof val === "object" && ("x" in val) && ("y" in val) && ("z" in val)) {
        c[k] = {x: val.x, y: val.y, z: val.z, _isVec: true};
      } else {
        c[k] = val;
      }
    }
  }
  activeCycleObj = {type: "onCycle", cycleType: cycleType, cycle: c, points: []};
  curSec.events.push(activeCycleObj);
};

onCyclePoint = function(x, y, z) {
  var start = getCurrentPosition();
  var first = isFirstCyclePoint();
  var last = isLastCyclePoint();
  var expMoves = [];
  var reposMoves = [];
  var afterReposMoves = [];

  capturingExpanded = reposMoves;
  try {
    repositionToCycleClearance(cycle, x, y, z);
  } catch (e) {}
  capturingExpanded = afterReposMoves;
  try {
    expandCyclePoint(x, y, z);
  } catch (e) {}
  capturingExpanded = null;

  setCurrentPosition(start);
  capturingExpanded = expMoves;
  try {
    expandCyclePoint(x, y, z);
  } catch (e) {}
  capturingExpanded = null;
  setCurrentPosition(new Vector(x, y, z));

  var pt = {
    type: "onCyclePoint", x: x, y: y, z: z,
    sx: start.x, sy: start.y, sz: start.z,
    first: first, last: last,
    reposMoves: reposMoves,
    afterReposMoves: afterReposMoves,
    expandedMoves: expMoves
  };
  if (activeCycleObj) {
    activeCycleObj.points.push({x: x, y: y, z: z});
  }
  curSec.events.push(pt);
};

onCycleEnd = function() {
  activeCycleObj = null;
  curSec.events.push({type: "onCycleEnd"});
};

onSectionEnd = function() {
  var endPos = getCurrentPosition();
  curSec.endPos = serializeVector(endPos);
  curSec.endSpindleSpeed = spindleSpeed;
  if (typeof currentSection !== "undefined" && currentSection) {
    if (currentSection.getFinalPosition) {
      curSec.finalPosition = serializeVector(currentSection.getFinalPosition());
    }
    if (currentSection.getGlobalFinalPosition) {
      curSec.globalFinalPosition = serializeVector(currentSection.getGlobalFinalPosition());
    }
    if (currentSection.getFinalToolAxis) {
      curSec.finalToolAxis = serializeVector(currentSection.getFinalToolAxis());
    }
    if (currentSection.getGlobalFinalToolAxis) {
      curSec.globalFinalToolAxis = serializeVector(currentSection.getGlobalFinalToolAxis());
    }
    if (currentSection.isMultiAxis && currentSection.isMultiAxis() && currentSection.getFinalToolAxisABC) {
      curSec.finalToolAxisABC = serializeVector(currentSection.getFinalToolAxisABC());
    }
  }
  curSec = null;
};

onClose = function() {
  // Write raw JSON without any comment or line filters
  writeln(JSON.stringify(dumpData));

  // Optionally copy the binary Compact-NCI intermediate file for bootstrap replay
  var targetCnc = "";
  try {
    targetCnc = getEnvironmentVariable("TARGET_INTERMEDIATE_CNC");
  } catch (e) {}
  if (!targetCnc) {
    try {
      var cfg = _getDumperConfig();
      if (cfg && cfg.targetIntermediateCnc) {
        targetCnc = cfg.targetIntermediateCnc;
      }
    } catch (e) {}
  }
  if (!targetCnc) {
    try {
      var outPath = getOutputPath();
      if (outPath) {
        targetCnc = outPath + ".intermediate.cnc";
      }
    } catch (ignored) {}
  }
  if (targetCnc) {
    try {
      var intermediateSrc = getIntermediatePath();
      if (intermediateSrc && FileSystem.isFile(intermediateSrc)) {
        if (FileSystem.isFile(targetCnc)) {
          try {
            FileSystem.remove(targetCnc);
          } catch (ignored) {}
        }
        FileSystem.copyFile(intermediateSrc, targetCnc);
      }
    } catch (e) {}
  }
};
