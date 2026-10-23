/**
 * Special post wrapper for capturing stock and tool geometry metadata for unittests.
 *
 * Dynamically wraps the stock vendor post, emitting ;META_STOCK: and ;META_OP: comments
 * and capturing the raw intermediate Compact-NCI (.cnc) file on close.
 */

description = "Capture Stock and Tools Test Wrapper";
vendor = "Universal CAM Verification";
legal = "Copyright (C) 2026. All rights reserved.";
certificationLevel = 2;
minimumRevision = 45000;

capabilities = CAPABILITY_TURNING;
extension = "cnc";

var baseCps = getEnvironmentVariable("BASE_CPS");
if (!baseCps) {
  baseCps = "centroid_turning.cps";
}
include(baseCps);

function writeStockMeta() {
  var wp = getWorkpiece();
  var dInner = 0;
  if (hasParameter("stock-diameter-inner")) {
    dInner = getParameter("stock-diameter-inner");
  } else if (hasParameter("job:stockDiameterInner")) {
    dInner = getParameter("job:stockDiameterInner");
  }
  var stockMeta = {
    zLower    : Number(spatialFormat.format(wp.lower.z)),
    zUpper    : Number(spatialFormat.format(wp.upper.z)),
    rOuter    : Number(spatialFormat.format(wp.upper.x)),
    rInner    : Number(spatialFormat.format(dInner * 0.5)),
    partZLower: hasParameter("part-lower-z") ? Number(spatialFormat.format(getParameter("part-lower-z"))) : undefined,
    partZUpper: hasParameter("part-upper-z") ? Number(spatialFormat.format(getParameter("part-upper-z"))) : undefined,
    partROuter: hasParameter("part-upper-x") ? Number(spatialFormat.format(getParameter("part-upper-x"))) : undefined
  };
  writeln(";META_STOCK: " + JSON.stringify(stockMeta));
}

function writeOperationMeta() {
  var comp = tool.getCompensationDisplacement ? tool.getCompensationDisplacement() : {x: 0, y: 0};
  var orientDeg = hasParameter("operation:toolOrientation") ?
    getParameter("operation:toolOrientation") : (currentSection.toolOrientation * 180 / Math.PI);
  var opMeta = {
    id          : currentSection.getId(),
    name        : hasParameter("operation-comment") ? getParameter("operation-comment") : "",
    tool        : tool.number,
    post        : (toolingData.toolPost == REAR) ? "REAR" : "FRONT",
    orientDeg   : Number(orientDeg.toFixed(4)),
    insertOrient: hasParameter("operation:toolInsertOrientation") ? getParameter("operation:toolInsertOrientation") : "",
    hand        : tool.hand || "",
    noseR       : Number(spatialFormat.format(tool.noseRadius || 0)),
    comp        : [Number(comp.x.toFixed(6)), Number(comp.y.toFixed(6))],
    toolType    : (tool.type != undefined) ? getToolTypeName(tool.type) : "",
    cutterSVG   : tool.getCutterProfileAsSVGPath ? tool.getCutterProfileAsSVGPath() : "",
    holderSVG   : tool.getHolderProfileAsSVGPath ? tool.getHolderProfileAsSVGPath() : ""
  };
  writeln(";META_OP: " + JSON.stringify(opMeta));
}

var baseOnSection = onSection;
onSection = function() {
  baseOnSection();
  if (isFirstSection()) {
    writeStockMeta();
  }
  writeOperationMeta();
};

var baseOnClose = onClose;
onClose = function() {
  if (baseOnClose) {
    baseOnClose();
  }
  var targetCnc = getEnvironmentVariable("TARGET_INTERMEDIATE_CNC");
  if (!targetCnc) {
    var outPath = getOutputPath();
    if (outPath) {
      targetCnc = outPath + ".intermediate.cnc";
    }
  }
  if (targetCnc) {
    try {
      var intermediateSrc = getIntermediatePath();
      if (intermediateSrc && FileSystem.isFile(intermediateSrc)) {
        FileSystem.copyFile(intermediateSrc, targetCnc);
      }
    } catch (e) {
      writeComment("CAPTURE ERROR: " + e.toString());
    }
  }
};
