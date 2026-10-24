/**
 * Universal Context & Event Replayer (universal_replayer.cps)
 *
 * Executes any unmodified vendor `.cps` post processor (`BASE_POST`) against a
 * combined JSON context (`CONTEXT_JSON`) produced by merging one or more
 * `universal_dumper.cps` outputs.
 *
 * Key capabilities:
 * - Evaluates the stock vendor `.cps` inside a lexical closure (`buildVendor`)
 *   that shadows read-only C++ Post Kernel globals (`currentSection`, `tool`,
 *   `movement`, `radiusCompensation`, `spindleSpeed`, `cycle`, `cycleType`,
 *   `cycleExpanded`, `programName`, `programComment`) while preserving direct
 *   access to all C++ Post Kernel APIs (`Vector`, `Matrix`, `BoundingBox`,
 *   `createFormat`, `createModal`, `createOutputVariable`, `writeBlock`, etc.).
 * - Synthesizes `getNumberOfSections()`, `getSection()`, `getToolTable()`,
 *   `getWorkpiece()`, `getProperty()`, `setProperty()`, `expandCyclePoint()`,
 *   `repositionToCycleClearance()`, and `linearize()` across merged multi-tool
 *   sections.
 * - Supports rapid promotion via either direct event annotation (`ev.promote = true`)
 *   or a `PROMOTED_MOVES_JSON` map (`"secId:x1,y1,z1->x2,y2,z2": {"minOccur": K}`).
 */

description = "Universal Context & Event Replayer";
vendor = "Universal";
certificationLevel = 2;
minimumRevision = 45000;
capabilities = CAPABILITY_TURNING | CAPABILITY_MILLING | CAPABILITY_INTERMEDIATE;
extension = "nc";

function toVec(v) {
  if (!v) return new Vector(0, 0, 0);
  return new Vector(v.x, v.y, v.z);
}

function toMat(m) {
  if (!m) return new Matrix();
  return new Matrix(toVec(m.right), toVec(m.up), toVec(m.forward));
}

function toBox(b) {
  if (!b) return new BoundingBox(new Vector(0, 0, 0), new Vector(0, 0, 0));
  return new BoundingBox(toVec(b.lower), toVec(b.upper));
}

function normInt(val) {
  var mmVal = (typeof unit !== "undefined" && unit == IN) ? (val * 25.4) : val;
  return Math.round(mmVal * 10000);
}

function makeVectorKey(secId, sx, sy, sz, ex, ey, ez) {
  return secId + ":" +
    normInt(sx) + "," + normInt(sy) + "," + normInt(sz) + "->" +
    normInt(ex) + "," + normInt(ey) + "," + normInt(ez);
}

function makeTool(t) {
  var obj = {};
  for (var k in t) {
    obj[k] = t[k];
  }
  obj.getNumber = function() { return t.number; };
  obj.getCompensationOffset = function() { return t.compensationOffset; };
  obj.getLengthOffset = function() { return t.lengthOffset; };
  obj.getDiameterOffset = function() { return t.diameterOffset; };
  obj.getTurret = function() { return t.turret; };
  obj.getType = function() { return t.type; };
  obj.getDescription = function() { return t._description; };
  obj.getComment = function() { return t._comment; };
  obj.getHolderDescription = function() { return t.holderDescription || ""; };
  obj.getDiameter = function() { return t.diameter; };
  obj.getTipAngle = function() { return t.tipAngle; };
  obj.getCornerRadius = function() { return t.cornerRadius; };
  obj.getFluteLength = function() { return t.fluteLength; };
  obj.getShoulderLength = function() { return t.shoulderLength; };
  obj.getShaftDiameter = function() { return t.shaftDiameter; };
  obj.getBodyLength = function() { return t.bodyLength; };
  obj.getOverallLength = function() { return t.overallLength; };
  obj.getHolderLength = function() { return t.holderLength; };
  obj.getNumberOfFlutes = function() { return t.numberOfFlutes; };
  obj.getSpindleRPM = function() { return t.spindleRPM; };
  obj.getSurfaceSpeed = function() { return t.surfaceSpeed; };
  obj.getMaximumSpindleSpeed = function() { return t.maximumSpindleSpeed; };
  obj.isClockwise = function() { return t.clockwise; };
  obj.getCoolant = function() { return t.coolant; };
  obj.getMaterial = function() { return t.material; };
  obj.getManualToolChange = function() { return t._manualToolChange; };
  obj.isManualToolChange = function() { return t._manualToolChange; };
  obj.hasBreakControl = function() { return !!t.breakControl; };
  obj.getSpindleMode = function() { return t._spindleMode; };
  obj.getTaperAngle = function() { return t._taperAngle; };
  obj.getThreadPitch = function() { return t._threadPitch; };
  obj.getTappingFeedrate = function() { return t._tappingFeedrate; };
  obj.isTurningTool = function() { return t._isTurningTool; };
  obj.isLiveTool = function() { return t._isLiveTool; };
  obj.getToolId = function() { return t._toolId; };
  obj.getInscribedCircleDiameter = function() { return t.inscribedCircleDiameter; };
  obj.getEdgeLength = function() { return t.edgeLength; };
  obj.getNoseRadius = function() { return t.noseRadius; };
  obj.getReliefAngle = function() { return t.reliefAngle; };
  obj.getThickness = function() { return t.thickness; };
  obj.getGrooveWidth = function() { return t.grooveWidth; };
  obj.getCrossSection = function() { return t.crossSection || ""; };
  obj.getTolerance = function() { return t.tolerance || ""; };
  obj.getHand = function() { return t.hand || ""; };
  obj.getClamping = function() { return t.clamping || ""; };
  obj.getExtent = function() { return toBox(t._extent); };
  return obj;
}

function makeSection(s, idx) {
  var toolObj = makeTool(s.tool);
  var wp = toMat(s.workPlane);
  var wo = toVec(s.workOrigin);
  return {
    id: idx,
    type: s.type,
    quality: s.quality || 0,
    jetMode: s.jetMode || 0,
    workOffset: s.workOffset,
    wcs: s.wcs,
    probeWorkOffset: s.probeWorkOffset,
    workOrigin: wo,
    workPlane: wp,
    spindle: s.spindle,
    feedMode: s.feedMode,
    toolOrientation: s.toolOrientation || 0,
    partCatcher: s.partCatcher,
    tailstock: s.tailstock,
    strategy: s.strategy,
    getId: function() { return idx; },
    getType: function() { return s.type; },
    getMachiningType: function() { return s.type; },
    getQuality: function() { return s.quality || 0; },
    getJetMode: function() { return s.jetMode || 0; },
    getWorkOffset: function() { return s.workOffset; },
    getWCS: function() { return s.wcs; },
    getProbeWorkOffset: function() { return s.probeWorkOffset; },
    getWorkOrigin: function() { return wo; },
    getGlobalWorkOrigin: function() { return wo; },
    getWorkPlane: function() { return wp; },
    getGlobalWorkPlane: function() { return wp; },
    getWCSOrigin: function() { return wo; },
    getWCSPlane: function() { return wp; },
    getSpindle: function() { return s.spindle; },
    getFeedMode: function() { return s.feedMode; },
    toolOrientation: s.toolOrientation || 0,
    getToolOrientation: function() { return s.toolOrientation || 0; },
    getPartCatcher: function() { return s.partCatcher; },
    getTailstock: function() { return s.tailstock; },
    getStrategy: function() { return s.strategy; },
    getTool: function() { return toolObj; },
    getInitialPosition: function() { return toVec(s.initialPosition); },
    getGlobalInitialPosition: function() { return toVec(s.globalInitialPosition || s.initialPosition); },
    getFinalPosition: function() { return toVec(s.finalPosition || s.endPos || s.initialPosition); },
    getGlobalFinalPosition: function() { return toVec(s.globalFinalPosition || s.finalPosition || s.endPos || s.globalInitialPosition || s.initialPosition); },
    getFirstPosition: function() { return toVec(s.initialPosition); },
    getGlobalPosition: function() { return toVec(s.globalInitialPosition || s.initialPosition); },
    getInitialToolAxis: function() { return toVec(s.initialToolAxis || {x: 0, y: 0, z: 1}); },
    getGlobalInitialToolAxis: function() { return toVec(s.globalInitialToolAxis || s.initialToolAxis || {x: 0, y: 0, z: 1}); },
    getInitialToolAxisABC: function() { return toVec(s.initialToolAxisABC || {x: 0, y: 0, z: 0}); },
    getFinalToolAxis: function() { return toVec(s.finalToolAxis || s.initialToolAxis || {x: 0, y: 0, z: 1}); },
    getGlobalFinalToolAxis: function() { return toVec(s.globalFinalToolAxis || s.globalInitialToolAxis || s.finalToolAxis || s.initialToolAxis || {x: 0, y: 0, z: 1}); },
    getFinalToolAxisABC: function() { return toVec(s.finalToolAxisABC || s.initialToolAxisABC || {x: 0, y: 0, z: 0}); },
    getABCByPreference: function() { return toVec(s.initialToolAxisABC || {x: 0, y: 0, z: 0}); },
    getForceToolChange: function() { return !!s.forceToolChange; },
    getMovements: function() { return s.movements || 0; },
    getPatternId: function() { return s.patternId || 0; },
    getInternalPatternId: function() { return s.internalPatternId || 0; },
    getNumberOfPatternInstances: function() { return 1; },
    isPatterned: function() { return !!s.isPatterned; },
    hasAnyCycle: function() { return !!s.hasAnyCycle; },
    hasCycle: function() { return !!s.hasAnyCycle; },
    isOptional: function() { return !!s.isOptional; },
    isMultiAxis: function() { return !!s.isMultiAxis; },
    isOptimizedForMachine: function() { return !!s.isOptimizedForMachine; },
    getOptimizedTCPMode: function() { return s.optimizedTCPMode || 0; },
    isConnectionSection: function() { return false; },
    hasRewind: function() { return false; },
    isCuttingMotionAwayFromRotary: function() { return false; },
    doesToolpathFitWithinLimits: function() { return true; },
    optimize3DPositionsByMachine: function() {},
    optimizeMachineAnglesByMachine: function() {},
    getGlobalZRange: function() {
      var minVal = (s.zRangeMin !== undefined) ? s.zRangeMin : 0;
      var maxVal = (s.zRangeMax !== undefined) ? s.zRangeMax : 0;
      return {
        getMinimum: function() { return minVal; },
        getMaximum: function() { return maxVal; },
        expandToRange: function(other) {
          if (other.getMinimum() < minVal) minVal = other.getMinimum();
          if (other.getMaximum() > maxVal) maxVal = other.getMaximum();
        }
      };
    },
    getZRange: function() {
      var minVal = (s.zRangeMin !== undefined) ? s.zRangeMin : 0;
      var maxVal = (s.zRangeMax !== undefined) ? s.zRangeMax : 0;
      return {
        getMinimum: function() { return minVal; },
        getMaximum: function() { return maxVal; },
        expandToRange: function(other) {
          if (other.getMinimum() < minVal) minVal = other.getMinimum();
          if (other.getMaximum() > maxVal) maxVal = other.getMaximum();
        }
      };
    },
    hasParameter: function(name) {
      return !!(s.params && Object.prototype.hasOwnProperty.call(s.params, name));
    },
    getParameter: function(name, def) {
      return (s.params && Object.prototype.hasOwnProperty.call(s.params, name)) ? s.params[name] : def;
    },
    getProperty: function(name) { return undefined; },
    checkGroup: function() { return false; }
  };
}

function hydrateCycle(c) {
  if (!c) return undefined;
  var res = {};
  for (var k in c) {
    var val = c[k];
    if (val && typeof val === "object" && val._isVec) {
      res[k] = new Vector(val.x, val.y, val.z);
    } else {
      res[k] = val;
    }
  }
  return res;
}

// Suppress C++ kernel callbacks on the bootstrap .cnc file until onClose()
function onOpen() {}
function onSection() {}
function onSpindleSpeed() {}
function onOrientateSpindle() {}
function onMovement() {}
function onFeedMode() {}
function onRadiusCompensation() {}
function onDwell() {}
function onCommand() {}
function onRapid() {}
function onLinear() {}
function onCircular() {}
function onCycle() {}
function onCyclePoint() {}
function onCycleEnd() {}
function onSectionEnd() {}

function onClose() {
  var jsonPath = getEnvironmentVariable("CONTEXT_JSON");
  var basePostPath = getEnvironmentVariable("BASE_POST");
  var promotedJsonPath = "";
  try {
    promotedJsonPath = getEnvironmentVariable("PROMOTED_MOVES_JSON");
  } catch (e) {}

  var data = JSON.parse(loadText(jsonPath, "utf-8"));
  var vendorCode = loadText(basePostPath, "utf-8");

  var authorizedMap = null;
  var hasPromotedMap = false;
  if (promotedJsonPath && FileSystem.isFile(promotedJsonPath)) {
    try {
      authorizedMap = JSON.parse(loadText(promotedJsonPath, "utf-8"));
      hasPromotedMap = true;
    } catch (e) {}
  }

  var sections = [];
  for (var i = 0; i < data.sections.length; ++i) {
    sections.push(makeSection(data.sections[i], i));
  }

  var uniqueTools = [];
  var seenToolNums = {};
  for (var ti = 0; ti < sections.length; ++ti) {
    var st = sections[ti].getTool();
    if (!seenToolNums[st.number]) {
      seenToolNums[st.number] = true;
      uniqueTools.push(st);
    }
  }

  var curPos = new Vector(0, 0, 0);
  var curEv = null;
  var curCyclePoints = [];
  var curSync = false;

  getToolTable = function() {
    return {
      getNumberOfTools: function() { return uniqueTools.length; },
      getTool: function(idx) { return uniqueTools[idx]; }
    };
  };
  getNumberOfSections = function() { return sections.length; };
  getSection = function(id) { return sections[id]; };
  hasGlobalParameter = function(name) {
    return Object.prototype.hasOwnProperty.call(data.globalParams, name);
  };
  getGlobalParameter = function(name, def) {
    return Object.prototype.hasOwnProperty.call(data.globalParams, name) ? data.globalParams[name] : def;
  };
  getWorkpiece = function() { return toBox(data.workpiece); };

  var nextEvIsMotion = true;
  getNextRecord = function() {
    return {
      isMotion: function() { return nextEvIsMotion; }
    };
  };
  getCurrentPosition = function() { return curPos; };
  setCurrentPosition = function(v) { curPos = new Vector(v.x, v.y, v.z); };
  isSpeedFeedSynchronizationActive = function() { return curSync; };

  getCircularPlane = function() { return curEv.plane; };
  getCircularRadius = function() { return curEv.radius; };
  getCircularSweep = function() { return curEv.sweep; };
  getCircularNormal = function() { return new Vector(curEv.nx, curEv.ny, curEv.nz); };
  getCircularStart = function() { return new Vector(curEv.sx, curEv.sy, curEv.sz); };
  getCircularCenter = function() { return new Vector(curEv.cx, curEv.cy, curEv.cz); };
  getCircularEnd = function() { return new Vector(curEv.x, curEv.y, curEv.z); };
  isFullCircle = function() { return !!curEv.fullCircle; };
  isHelical = function() { return !!curEv.helical; };
  isSpiral = function() { return !!curEv.spiral; };

  isFirstCyclePoint = function() { return !!curEv.first; };
  isLastCyclePoint = function() { return !!curEv.last; };
  getNumberOfCyclePoints = function() { return curCyclePoints.length; };
  getCyclePoint = function(i) { return toVec(curCyclePoints[i]); };

  __getCurEv = function() { return curEv; };
  __setPos = function(v) { curPos = v; };

  unit = data.unit;
  if (data.highFeedrate) {
    highFeedrate = data.highFeedrate;
  }

  var buildVendor = new Function(
    "writeln",
    "writeWords",
    "writeWords2",
    "var currentSection, tool, movement, radiusCompensation, spindleSpeed, cycle, cycleType, cycleExpanded, programName, programComment;\n" +
    "var onOpen, onComment, onPassThrough, onParameter, onSection, onSpindleSpeed, onOrientateSpindle, onMovement, onFeedMode, onRadiusCompensation, onDwell, onCommand, onRapid, onLinear, onCircular, onCycle, onCyclePoint, onCycleEnd, onSectionEnd, onClose;\n" +
    "var __didRepos = false;\n" +
    "function getCurrentSectionId() { return currentSection.getId(); }\n" +
    "function isFirstSection() { return currentSection.getId() === 0; }\n" +
    "function isLastSection() { return currentSection.getId() === (getNumberOfSections() - 1); }\n" +
    "function hasPreviousSection() { return currentSection.getId() > 0; }\n" +
    "function hasNextSection() { return (currentSection.getId() + 1) < getNumberOfSections(); }\n" +
    "function getPreviousSection() {\n" +
    "  if (!currentSection || currentSection.getId() <= 0) return currentSection;\n" +
    "  return getSection(currentSection.getId() - 1);\n" +
    "}\n" +
    "function getNextSection() {\n" +
    "  if (!currentSection || (currentSection.getId() + 1) >= getNumberOfSections()) return currentSection;\n" +
    "  return getSection(currentSection.getId() + 1);\n" +
    "}\n" +
    "function isNewWorkPlane(sec) {\n" +
    "  var section = (sec && typeof sec === 'object') ? sec : currentSection;\n" +
    "  if (!section) return false;\n" +
    "  var firstSection = section.getId() === 0;\n" +
    "  var newWorkPlane = firstSection;\n" +
    "  if (!firstSection) {\n" +
    "    var previousSection = getSection(section.getId() - 1);\n" +
    "    if (previousSection) {\n" +
    "      var isOptimized = section.isOptimizedForMachine() && previousSection.isOptimizedForMachine();\n" +
    "      newWorkPlane = newWorkPlane || !isSameDirection(previousSection.getGlobalFinalToolAxis(), section.getGlobalInitialToolAxis());\n" +
    "      newWorkPlane = newWorkPlane || (isOptimized && Vector.diff(previousSection.getFinalToolAxisABC(), section.getInitialToolAxisABC()).length > 1e-4);\n" +
    "      newWorkPlane = newWorkPlane || (typeof machineConfiguration !== 'undefined' && machineConfiguration && !machineConfiguration.isMultiAxisConfiguration() && section.isMultiAxis());\n" +
    "      newWorkPlane = newWorkPlane || (previousSection.isMultiAxis() !== section.isMultiAxis());\n" +
    "    }\n" +
    "  }\n" +
    "  return newWorkPlane;\n" +
    "}\n" +
    "function isNewWorkOffset(sec) {\n" +
    "  var section = (sec && typeof sec === 'object') ? sec : currentSection;\n" +
    "  if (!section) return false;\n" +
    "  var firstSection = section.getId() === 0;\n" +
    "  var newWorkOffset = firstSection;\n" +
    "  if (!firstSection) {\n" +
    "    var previousSection = getSection(section.getId() - 1);\n" +
    "    if (previousSection) {\n" +
    "      newWorkOffset = newWorkOffset || (previousSection.workOffset !== section.workOffset);\n" +
    "    }\n" +
    "  }\n" +
    "  return newWorkOffset;\n" +
    "}\n" +
    "function isToolChangeNeeded() {\n" +
    "  var start = (arguments.length > 0 && typeof arguments[0] === 'object') ? 1 : 0;\n" +
    "  var section = (arguments.length > 0 && typeof arguments[0] === 'object') ? arguments[0] : currentSection;\n" +
    "  if (!section) return false;\n" +
    "  var firstSection = section.getId() === 0;\n" +
    "  var currentTool = section.getTool();\n" +
    "  if (firstSection || section.getForceToolChange()) return true;\n" +
    "  var previousSection = getSection(section.getId() - 1);\n" +
    "  if (!previousSection) return true;\n" +
    "  var previousTool = previousSection.getTool();\n" +
    "  if (!previousTool) return true;\n" +
    "  if (arguments.length === start) {\n" +
    "    return currentTool.number !== previousTool.number;\n" +
    "  } else {\n" +
    "    for (var i = start; i < arguments.length; ++i) {\n" +
    "      var prop = arguments[i];\n" +
    "      if (previousTool[prop] !== currentTool[prop]) return true;\n" +
    "    }\n" +
    "  }\n" +
    "  return false;\n" +
    "}\n" +
    "function getNextTool() {\n" +
    "  var start = (arguments.length > 0 && typeof arguments[0] === 'object') ? 1 : 0;\n" +
    "  var section = (arguments.length > 0 && typeof arguments[0] === 'object') ? arguments[0] : currentSection;\n" +
    "  if (!section) return undefined;\n" +
    "  var currentSectionId = section.getId();\n" +
    "  var numSections = getNumberOfSections();\n" +
    "  var returnFirstTool = (arguments.length > start && typeof arguments[start] === 'boolean') ? arguments[start] : false;\n" +
    "  for (var i = currentSectionId + 1; i < numSections; ++i) {\n" +
    "    var nextSec = getSection(i);\n" +
    "    if (nextSec && nextSec.getTool().number !== section.getTool().number) {\n" +
    "      return nextSec.getTool();\n" +
    "    }\n" +
    "  }\n" +
    "  return returnFirstTool ? getSection(0).getTool() : undefined;\n" +
    "}\n" +
    "function isSpindleSpeedDifferent(sec) {\n" +
    "  var section = (sec && typeof sec === 'object') ? sec : currentSection;\n" +
    "  if (!section || section.getId() === 0) return true;\n" +
    "  var previousSection = getSection(section.getId() - 1);\n" +
    "  if (!previousSection) return true;\n" +
    "  if (previousSection.getTool().clockwise !== section.getTool().clockwise) return true;\n" +
    "  if (section.getTool().getSpindleMode() === SPINDLE_CONSTANT_SURFACE_SPEED) {\n" +
    "    return (previousSection.getTool().getSpindleMode() !== SPINDLE_CONSTANT_SURFACE_SPEED) ||\n" +
    "      Math.round(previousSection.getTool().surfaceSpeed - section.getTool().surfaceSpeed) !== 0;\n" +
    "  } else {\n" +
    "    return (previousSection.getTool().getSpindleMode() !== SPINDLE_CONSTANT_SPINDLE_SPEED) ||\n" +
    "      Math.round(previousSection.getTool().spindleRPM - section.getTool().spindleRPM) !== 0;\n" +
    "  }\n" +
    "}\n" +
    "function isProbeOperation(sec) {\n" +
    "  var section = (sec && typeof sec === 'object') ? sec : currentSection;\n" +
    "  if (!section) return false;\n" +
    "  var strat = section.getParameter('operation-strategy', '');\n" +
    "  return strat === 'probe' || strat === 'probe_geometry';\n" +
    "}\n" +
    "function isDrillingCycle(sec) {\n" +
    "  var section = (sec && typeof sec === 'object') ? sec : currentSection;\n" +
    "  if (!section) return false;\n" +
    "  return section.strategy === 'drill';\n" +
    "}\n" +
    "function hasParameter(name) { return (currentSection && currentSection.hasParameter(name)) || hasGlobalParameter(name); }\n" +
    "function getParameter(name, def) { return (currentSection && currentSection.hasParameter(name)) ? currentSection.getParameter(name, def) : getGlobalParameter(name, def); }\n" +
    "function getProperty(name, def) {\n" +
    "  if (typeof properties !== 'undefined' && properties && Object.prototype.hasOwnProperty.call(properties, name)) {\n" +
    "    var p = properties[name];\n" +
    "    if (p !== null && typeof p === 'object') {\n" +
    "      if ('current' in p && p.current !== undefined) return p.current;\n" +
    "      if ('value' in p) return p.value;\n" +
    "    }\n" +
    "    return p;\n" +
    "  }\n" +
    "  return def;\n" +
    "}\n" +
    "function setProperty(name, val) {\n" +
    "  if (typeof properties !== 'undefined' && properties && Object.prototype.hasOwnProperty.call(properties, name)) {\n" +
    "    if (properties[name] !== null && typeof properties[name] === 'object') {\n" +
    "      properties[name].value = val;\n" +
    "      properties[name].current = val;\n" +
    "    } else {\n" +
    "      properties[name] = val;\n" +
    "    }\n" +
    "  }\n" +
    "}\n" +
    "function __applyPostProp(name, val) {\n" +
    "  if (typeof properties !== 'undefined' && properties && Object.prototype.hasOwnProperty.call(properties, name)) {\n" +
    "    if (properties[name] !== null && typeof properties[name] === 'object') {\n" +
    "      properties[name].value = val;\n" +
    "      properties[name].current = val;\n" +
    "    } else {\n" +
    "      properties[name] = val;\n" +
    "    }\n" +
    "  }\n" +
    "  if (typeof setProperty === 'function') {\n" +
    "    try { setProperty(name, val); } catch (e) {}\n" +
    "  }\n" +
    "}\n" +
    vendorCode + "\n" +
    "if (typeof gMotionModal !== 'undefined') this.gMotionModal = gMotionModal;\n" +
    "if (typeof gPlaneModal !== 'undefined') this.gPlaneModal = gPlaneModal;\n" +
    "if (typeof gAbsIncModal !== 'undefined') this.gAbsIncModal = gAbsIncModal;\n" +
    "if (typeof gFeedModeModal !== 'undefined') this.gFeedModeModal = gFeedModeModal;\n" +
    "if (typeof feedOutput !== 'undefined') this.feedOutput = feedOutput;\n" +
    "if (typeof fourthAxisClamp !== 'undefined') this.fourthAxisClamp = fourthAxisClamp;\n" +
    "if (typeof fifthAxisClamp !== 'undefined') this.fifthAxisClamp = fifthAxisClamp;\n" +
    "if (typeof sixthAxisClamp !== 'undefined') this.sixthAxisClamp = sixthAxisClamp;\n" +
    "function linearize(tol) {\n" +
    "  var cEv = __getCurEv();\n" +
    "  if (cEv && cEv.linearizedMoves && cEv.linearizedMoves.length > 0) {\n" +
    "    for (var m = 0; m < cEv.linearizedMoves.length; ++m) {\n" +
    "      var mv = cEv.linearizedMoves[m];\n" +
    "      if (mv.movement !== undefined) movement = mv.movement;\n" +
    "      onLinear(mv.x, mv.y, mv.z, mv.feed);\n" +
    "      __setPos(new Vector(mv.x, mv.y, mv.z));\n" +
    "    }\n" +
    "  } else if (cEv) {\n" +
    "    onLinear(cEv.x, cEv.y, cEv.z, cEv.feed);\n" +
    "    __setPos(new Vector(cEv.x, cEv.y, cEv.z));\n" +
    "  }\n" +
    "}\n" +
    "function expandCyclePoint(x, y, z) {\n" +
    "  cycleExpanded = true;\n" +
    "  var cEv = __getCurEv();\n" +
    "  var list = (cEv && __didRepos) ? cEv.afterReposMoves : (cEv ? cEv.expandedMoves : null);\n" +
    "  if (list) {\n" +
    "    for (var m = 0; m < list.length; ++m) {\n" +
    "      var mv = list[m];\n" +
    "      if (mv.movement !== undefined) movement = mv.movement;\n" +
    "      if (mv.type === 'onRapid') {\n" +
    "        onRapid(mv.x, mv.y, mv.z);\n" +
    "        __setPos(new Vector(mv.x, mv.y, mv.z));\n" +
    "      } else if (mv.type === 'onLinear') {\n" +
    "        onLinear(mv.x, mv.y, mv.z, mv.feed);\n" +
    "        __setPos(new Vector(mv.x, mv.y, mv.z));\n" +
    "      } else if (mv.type === 'onDwell') {\n" +
    "        if (typeof onDwell === 'function') onDwell(mv.sec);\n" +
    "      }\n" +
    "    }\n" +
    "  }\n" +
    "}\n" +
    "function repositionToCycleClearance(cycle, x, y, z) {\n" +
    "  __didRepos = true;\n" +
    "  var cEv = __getCurEv();\n" +
    "  if (cEv && cEv.reposMoves) {\n" +
    "    for (var m = 0; m < cEv.reposMoves.length; ++m) {\n" +
    "      var mv = cEv.reposMoves[m];\n" +
    "      if (mv.movement !== undefined) movement = mv.movement;\n" +
    "      if (mv.type === 'onRapid') {\n" +
    "        onRapid(mv.x, mv.y, mv.z);\n" +
    "        __setPos(new Vector(mv.x, mv.y, mv.z));\n" +
    "      } else if (mv.type === 'onLinear') {\n" +
    "        onLinear(mv.x, mv.y, mv.z, mv.feed);\n" +
    "        __setPos(new Vector(mv.x, mv.y, mv.z));\n" +
    "      }\n" +
    "    }\n" +
    "  }\n" +
    "}\n" +
    "return {\n" +
    "  setGlobals: function(pn, pc) { programName = pn; programComment = pc; },\n" +
    "  setState: function(st) {\n" +
    "    if ('currentSection' in st) currentSection = st.currentSection;\n" +
    "    if ('tool' in st) tool = st.tool;\n" +
    "    if ('movement' in st) movement = st.movement;\n" +
    "    if ('radiusCompensation' in st) radiusCompensation = st.radiusCompensation;\n" +
    "    if ('spindleSpeed' in st) spindleSpeed = st.spindleSpeed;\n" +
    "    if ('cycle' in st) cycle = st.cycle;\n" +
    "    if ('cycleType' in st) cycleType = st.cycleType;\n" +
    "    if ('cycleExpanded' in st) cycleExpanded = st.cycleExpanded;\n" +
    "  },\n" +
    "  setProperty: setProperty,\n" +
    "  applyPostProp: __applyPostProp,\n" +
    "  formatComment: (typeof formatComment === 'function') ? formatComment : null,\n" +
    "  writeComment: (typeof writeComment === 'function') ? writeComment : null,\n" +
    "  onOpen: (typeof onOpen === 'function') ? onOpen : null,\n" +
    "  onComment: (typeof onComment === 'function') ? onComment : null,\n" +
    "  onPassThrough: (typeof onPassThrough === 'function') ? onPassThrough : null,\n" +
    "  onParameter: (typeof onParameter === 'function') ? onParameter : null,\n" +
    "  onSection: (typeof onSection === 'function') ? onSection : null,\n" +
    "  onSpindleSpeed: (typeof onSpindleSpeed === 'function') ? onSpindleSpeed : null,\n" +
    "  onOrientateSpindle: (typeof onOrientateSpindle === 'function') ? onOrientateSpindle : null,\n" +
    "  onMovement: (typeof onMovement === 'function') ? onMovement : null,\n" +
    "  onFeedMode: (typeof onFeedMode === 'function') ? onFeedMode : null,\n" +
    "  onRadiusCompensation: (typeof onRadiusCompensation === 'function') ? onRadiusCompensation : null,\n" +
    "  onDwell: (typeof onDwell === 'function') ? onDwell : null,\n" +
    "  onCommand: (typeof onCommand === 'function') ? onCommand : null,\n" +
    "  onRapid: (typeof onRapid === 'function') ? onRapid : null,\n" +
    "  onLinear: (typeof onLinear === 'function') ? onLinear : null,\n" +
    "  onCircular: (typeof onCircular === 'function') ? onCircular : null,\n" +
    "  onCycle: (typeof onCycle === 'function') ? onCycle : null,\n" +
    "  onCyclePoint: (typeof onCyclePoint === 'function') ? function(x,y,z){ __didRepos = false; onCyclePoint(x,y,z); } : function(x,y,z){ __didRepos = false; expandCyclePoint(x,y,z); },\n" +
    "  onCycleEnd: (typeof onCycleEnd === 'function') ? onCycleEnd : null,\n" +
    "  onSectionEnd: (typeof onSectionEnd === 'function') ? onSectionEnd : null,\n" +
    "  onClose: (typeof onClose === 'function') ? onClose : null\n" +
    "};"
  );

  var _origWriteln = writeln;
  var _origWriteWords = writeWords;
  var _origWriteWords2 = writeWords2;
  var _promoCommentSuffix = "";
  var _customWriteln = function(text) {
    if (_promoCommentSuffix && typeof text === "string" && text.trim().length > 0) {
      var sfx = _promoCommentSuffix;
      _promoCommentSuffix = "";
      _origWriteln(text + " " + sfx);
    } else {
      _origWriteln(text);
    }
  };
  var _customWriteWords = function() {
    var args = Array.prototype.slice.call(arguments);
    if (_promoCommentSuffix) {
      var sfx = _promoCommentSuffix;
      _promoCommentSuffix = "";
      args.push(sfx);
    }
    _origWriteWords.apply(null, args);
  };
  var _customWriteWords2 = function() {
    var args = Array.prototype.slice.call(arguments);
    if (_promoCommentSuffix) {
      var sfx = _promoCommentSuffix;
      _promoCommentSuffix = "";
      args.push(sfx);
    }
    _origWriteWords2.apply(null, args);
  };

  var vendor = buildVendor(_customWriteln, _customWriteWords, _customWriteWords2);
  vendor.setGlobals(data.programName, data.programComment);

  if (data.postProperties && typeof data.postProperties === "object" && vendor.applyPostProp) {
    for (var ppk in data.postProperties) {
      if (Object.prototype.hasOwnProperty.call(data.postProperties, ppk)) {
        try {
          vendor.applyPostProp(ppk, data.postProperties[ppk]);
        } catch (e) {}
      }
    }
  }

  for (var gk in data.globalParams) {
    if (vendor.onParameter) vendor.onParameter(gk, data.globalParams[gk]);
  }

  if (sections.length > 0) {
    try {
      currentSection = sections[0];
      tool = sections[0].getTool();
    } catch (e) {}
    vendor.setState({
      currentSection: sections[0],
      tool: sections[0].getTool(),
      spindleSpeed: data.sections[0].initSpindleSpeed,
      movement: data.sections[0].initMovement,
      radiusCompensation: data.sections[0].initRadiusComp
    });
  }

  if (vendor.onOpen) vendor.onOpen();

  for (var ci = 0; ci < data.comments.length; ++ci) {
    if (vendor.onComment) vendor.onComment(data.comments[ci]);
  }

  var occurCounts = {};
  var restoredCount = 0;
  var inCycle = false;

  for (var si = 0; si < sections.length; ++si) {
    var sec = sections[si];
    var rawSec = data.sections[si];

    for (var pk in rawSec.params) {
      if (vendor.onParameter) vendor.onParameter(pk, rawSec.params[pk]);
    }

    try {
      currentSection = sec;
      tool = sec.getTool();
    } catch (e) {}

    vendor.setState({
      currentSection: sec,
      tool: sec.getTool(),
      spindleSpeed: rawSec.initSpindleSpeed,
      movement: rawSec.initMovement,
      radiusCompensation: rawSec.initRadiusComp,
      cycle: undefined,
      cycleType: "",
      cycleExpanded: false
    });
    curPos = sec.getInitialPosition();
    inCycle = false;

    if (vendor.onSection) vendor.onSection();

    for (var ei = 0; ei < rawSec.events.length; ++ei) {
      var ev = rawSec.events[ei];
      curEv = ev;
      nextEvIsMotion = false;
      for (var nei = ei + 1; nei < rawSec.events.length; ++nei) {
        var nt = rawSec.events[nei].type;
        if (nt === "onRapid" || nt === "onLinear" || nt === "onCircular" || nt === "onCyclePoint") {
          nextEvIsMotion = true;
          break;
        }
      }

      switch (ev.type) {
        case "onComment":
          if (vendor.onComment) vendor.onComment(ev.c);
          break;
        case "onPassThrough":
          if (vendor.onPassThrough) vendor.onPassThrough(ev.text);
          break;
        case "onParameter":
          if (vendor.onParameter) vendor.onParameter(ev.name, ev.value);
          break;
        case "onSpindleSpeed":
          vendor.setState({spindleSpeed: ev.s});
          if (vendor.onSpindleSpeed) vendor.onSpindleSpeed(ev.s);
          break;
        case "onOrientateSpindle":
          if (vendor.onOrientateSpindle) vendor.onOrientateSpindle(ev.angle);
          break;
        case "onMovement":
          vendor.setState({movement: ev.m});
          if (vendor.onMovement) vendor.onMovement(ev.m);
          break;
        case "onFeedMode":
          if (vendor.onFeedMode) vendor.onFeedMode(ev.fm);
          break;
        case "onRadiusCompensation":
          vendor.setState({radiusCompensation: ev.rc});
          if (vendor.onRadiusCompensation) vendor.onRadiusCompensation();
          break;
        case "onDwell":
          if (vendor.onDwell) vendor.onDwell(ev.sec);
          break;
        case "onCommand":
          if (vendor.onCommand) vendor.onCommand(ev.cmd);
          break;
        case "onRapid":
          curPos = new Vector(ev.sx, ev.sy, ev.sz);
          vendor.setState({movement: ev.movement, radiusCompensation: ev.rc, spindleSpeed: ev.rpm});
          if (vendor.onRapid) vendor.onRapid(ev.x, ev.y, ev.z);
          curPos = new Vector(ev.x, ev.y, ev.z);
          break;
        case "onLinear":
          curPos = new Vector(ev.sx, ev.sy, ev.sz);
          curSync = !!ev.sync;
          vendor.setState({movement: ev.movement, radiusCompensation: ev.rc, spindleSpeed: ev.rpm});
          var shouldPromote = !!ev.promote;
          if (!shouldPromote && !inCycle && authorizedMap) {
            var vKey = makeVectorKey(si, ev.sx, ev.sy, ev.sz, ev.x, ev.y, ev.z);
            var rule = authorizedMap[vKey];
            if (!rule && rawSec.origSecId !== undefined) {
              var localKey = makeVectorKey(rawSec.origSecId, ev.sx, ev.sy, ev.sz, ev.x, ev.y, ev.z);
              rule = authorizedMap[localKey];
              if (rule) vKey = localKey;
            }
            if (rule) {
              var occ = (occurCounts[vKey] || 0) + 1;
              occurCounts[vKey] = occ;
              var minOcc = (typeof rule === "object" && rule !== null && rule.minOccur !== undefined) ? rule.minOccur : 1;
              if (occ >= minOcc) {
                shouldPromote = true;
              }
            }
          }
          if (shouldPromote && !inCycle) {
            restoredCount += 1;
            _promoCommentSuffix = (typeof vendor.formatComment === "function")
              ? vendor.formatComment("Rapid restored")
              : "(Rapid restored)";
            try {
              if (vendor.onRapid) vendor.onRapid(ev.x, ev.y, ev.z);
            } finally {
              _promoCommentSuffix = "";
            }
          } else {
            if (vendor.onLinear) vendor.onLinear(ev.x, ev.y, ev.z, ev.feed);
          }
          curPos = new Vector(ev.x, ev.y, ev.z);
          curSync = false;
          break;
        case "onCircular":
          curPos = new Vector(ev.sx, ev.sy, ev.sz);
          vendor.setState({movement: ev.movement, radiusCompensation: ev.rc, spindleSpeed: ev.rpm});
          if (vendor.onCircular) {
            vendor.onCircular(ev.cw, ev.cx, ev.cy, ev.cz, ev.x, ev.y, ev.z, ev.feed);
          }
          curPos = new Vector(ev.x, ev.y, ev.z);
          break;
        case "onCycle":
          inCycle = true;
          curCyclePoints = ev.points || [];
          vendor.setState({cycleType: ev.cycleType, cycle: hydrateCycle(ev.cycle), cycleExpanded: false});
          if (vendor.onCycle) vendor.onCycle();
          break;
        case "onCyclePoint":
          curPos = new Vector(ev.sx, ev.sy, ev.sz);
          if (vendor.onCyclePoint) vendor.onCyclePoint(ev.x, ev.y, ev.z);
          curPos = new Vector(ev.x, ev.y, ev.z);
          break;
        case "onCycleEnd":
          if (vendor.onCycleEnd) vendor.onCycleEnd();
          vendor.setState({cycle: undefined, cycleType: "", cycleExpanded: false});
          curCyclePoints = [];
          inCycle = false;
          break;
      }
    }

    curPos = toVec(rawSec.endPos);
    vendor.setState({spindleSpeed: rawSec.endSpindleSpeed});
    if (vendor.onSectionEnd) vendor.onSectionEnd();

    for (var pci = 0; pci < rawSec.postComments.length; ++pci) {
      if (vendor.onComment) vendor.onComment(rawSec.postComments[pci]);
    }
  }

  if (vendor.onClose) vendor.onClose();

  if (hasPromotedMap && vendor.writeComment) {
    try {
      vendor.writeComment("UNIVERSAL WRAPPER: RESTORED " + restoredCount + " G0 RAPID MOVES");
    } catch (e) {}
  }
}
