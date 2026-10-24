/**
 * APT Extractor & Intermediate Compact-NCI Capture Wrapper.
 *
 * Dynamically wraps Autodesk's standard apt.cps to output an ISO 4343 .apt toolpath,
 * while intercepting getIntermediatePath() in onClose() to grab the raw binary
 * Compact-NCI (.cnc) file in a single Fusion CAM post invocation.
 *
 * Hardened for multi-tool turning operations:
 * - Overrides capabilities to ensure CAPABILITY_TURNING is present for Turning setups.
 * - Wraps numeric formatters (mainFormat, ijkFormat) to prevent NaN/undefined formatting exceptions.
 * - Inoculates writeCutter and onSection for turning insert geometries lacking milling parameters.
 */

description = "APT Extractor & Compact-NCI Capture Wrapper";
vendor = "Paterson Technology / Universal";
legal = "Copyright (C) 2026. All rights reserved.";
certificationLevel = 2;
minimumRevision = 45000;

capabilities = CAPABILITY_TURNING | CAPABILITY_MILLING | CAPABILITY_INTERMEDIATE;
extension = "apt";

function _logCPS(msg) {
  try {
    var f = new TextFile("/tmp/postprocess_debug.log", true, "ansi");
    f.writeln("[CPS " + (new Date()).toISOString() + "] " + msg);
    f.close();
  } catch (e) {}
}

_logCPS("apt_extractor_wrapper.cps evaluated");

var systemAptPath = "";
try {
  systemAptPath = getEnvironmentVariable("SYSTEM_APT_CPS");
} catch (e) {}

if (!systemAptPath || !FileSystem.isFile(systemAptPath)) {
  var candidates = [
    "/Users/vadimf/Documents/Fusion360-Batch-Post-master/apt.cps",
    "/Users/vadimf/Library/Application Support/Autodesk/Fusion 360 CAM/Posts/apt.cps",
    "/Users/vadimf/Library/Application Support/Autodesk/webdeploy/production/c139e2731e42a0340acc3d34de2cbdb01237a27c/Autodesk Fusion.app/Contents/Libraries/Applications/CAM360/Data/Posts/apt.cps"
  ];
  try {
    var cfgFolder = FileSystem.getFolderPath(getConfigurationPath());
    if (cfgFolder) {
      candidates.unshift(FileSystem.getCombinedPath(cfgFolder, "apt.cps"));
    }
  } catch (e) {}

  for (var i = 0; i < candidates.length; ++i) {
    var c = candidates[i];
    if (c && FileSystem.isFile(c) && (c.indexOf("/") === 0 || c.indexOf(":") === 1)) {
      systemAptPath = c;
      break;
    }
  }
}

if (!systemAptPath) {
  systemAptPath = "apt.cps";
}

// Dynamically include Autodesk system apt.cps
include(systemAptPath);

// Re-assert capabilities and metadata overridden by included apt.cps
capabilities = CAPABILITY_TURNING | CAPABILITY_MILLING | CAPABILITY_INTERMEDIATE;
extension = "apt";
description = "APT Extractor & Compact-NCI Capture Wrapper";
vendor = "Paterson Technology / Universal";

// Wrap formatters to prevent crash on undefined or NaN numbers (common with turning tools)
function wrapFormat(fmt) {
  if (!fmt || typeof fmt.format !== "function") return;
  var orig = fmt.format;
  fmt.format = function(val) {
    if (typeof val !== "number" || isNaN(val) || !isFinite(val)) {
      val = 0.0;
    }
    return orig.call(fmt, val);
  };
}

wrapFormat(mainFormat);
wrapFormat(ijkFormat);

var baseOnOpen = (typeof onOpen === "function") ? onOpen : null;
onOpen = function() {
  _logCPS("onOpen called");
  if (baseOnOpen) {
    baseOnOpen();
  }
  wrapFormat(mainFormat);
  wrapFormat(ijkFormat);
};

// Override writeCutter with fully safe handling for turning & milling tools
writeCutter = function() {
  try {
    var d = 0.0;
    var r = 0.0;
    var e = 0.0;
    var b = 0.0;
    var h = 0.0;
    if (typeof tool !== "undefined" && tool) {
      d = (typeof tool.diameter === "number" && !isNaN(tool.diameter)) ? tool.diameter : 0.0;
      r = (typeof tool.cornerRadius === "number" && !isNaN(tool.cornerRadius)) ? tool.cornerRadius : 0.0;
      e = (typeof tool.tipDiameter === "number" && !isNaN(tool.tipDiameter)) ? tool.tipDiameter : 0.0;
      b = (typeof tool.taperAngle === "number" && !isNaN(tool.taperAngle)) ? tool.taperAngle : 0.0;
      h = (typeof tool.fluteLength === "number" && !isNaN(tool.fluteLength)) ? tool.fluteLength : 0.0;

      if (tool.type == TOOL_DRILL || tool.type == TOOL_DRILL_CENTER || tool.type == TOOL_DRILL_SPOT) {
        b /= 2.0;
      }
      if (b > 0 && b < Math.PI / 2) {
        d = e + r - (2 * r) + (2 * (r * Math.tan((Math.PI / 2 - b) / 2)));
      }
    }
    writeln("CUTTER/" + mainFormat.format(d) + ", " + mainFormat.format(r) +
      ", " + mainFormat.format(e) + ", " + mainFormat.format(r) +
      ", 0.0, " + mainFormat.format(toDeg(b)) + ", " + mainFormat.format(h)
    );
  } catch (err) {
    writeln("CUTTER/1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0");
  }
};

// Hook onClose to capture the intermediate Compact-NCI file
var baseOnClose = (typeof onClose === "function") ? onClose : null;

onClose = function() {
  _logCPS("onClose called");
  if (baseOnClose) {
    try {
      baseOnClose();
    } catch (e) {}
  }

  var targetCnc = "";
  try {
    targetCnc = getEnvironmentVariable("TARGET_INTERMEDIATE_CNC");
  } catch (e) {}

  if (!targetCnc) {
    try {
      var outPath = getOutputPath();
      if (outPath) {
        targetCnc = outPath + ".intermediate.cnc";
      }
    } catch (ignored) {}
  }

  _logCPS("onClose targetCnc=" + targetCnc);
  if (targetCnc) {
    try {
      var intermediateSrc = getIntermediatePath();
      _logCPS("onClose intermediateSrc=" + intermediateSrc);
      if (intermediateSrc && FileSystem.isFile(intermediateSrc)) {
        if (FileSystem.isFile(targetCnc)) {
          try {
            FileSystem.remove(targetCnc);
          } catch (ignored) {}
        }
        FileSystem.copyFile(intermediateSrc, targetCnc);
        _logCPS("onClose copyFile succeeded to " + targetCnc);
      }
    } catch (e) {
      _logCPS("onClose copyFile failed: " + e.toString());
      writeComment("APT EXTRACTOR ERROR: " + e.toString());
    }
  }
};
