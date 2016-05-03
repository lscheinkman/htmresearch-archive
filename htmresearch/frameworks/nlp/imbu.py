# ----------------------------------------------------------------------
# Numenta Platform for Intelligent Computing (NuPIC)
# Copyright (C) 2016, Numenta, Inc.  Unless you have purchased from
# Numenta, Inc. a separate commercial license for this software code, the
# following terms and conditions apply:
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU Affero Public License for more details.
#
# You should have received a copy of the GNU Affero Public License
# along with this program.  If not, see http://www.gnu.org/licenses.
#
# http://numenta.org/licenses/
# ----------------------------------------------------------------------

"""
Class for running models in Imbu app.

The script is runnable to demo Imbu functionality. Example invocations (from
the repo's base dir):

  python htmresearch/frameworks/nlp/imbu.py \
    -d projects/nlp/data/sample_reviews/sample_reviews.csv \
    -m Keywords

  python htmresearch/frameworks/nlp/imbu.py \
    -d projects/nlp/data/sample_reviews/sample_reviews_unlabeled.csv \
    -m HTM_sensor_tm_simple_tp_knn -c imbu_sensor_tm_simple_tp_knn.json \
    --savePath imbu_sensor_tm_simple_tp_knn.checkpoint
    # And then run again w/o training by specifying the loadPath
"""

import argparse
import copy
import numpy
import os
import pprint
import string
from tqdm import tqdm

from htmresearch.encoders import EncoderTypes
from htmresearch.frameworks.nlp.classification_model import ClassificationModel
from htmresearch.support.csv_helper import readCSV
from htmresearch.support.register_regions import registerAllResearchRegions
from htmresearch.frameworks.nlp.model_factory import (
  ClassificationModelTypes,
  createModel,
  getNetworkConfig)



class ModelSimilarityMetrics(object):
  pctOverlapOfInput = "pctOverlapOfInput"
  rawOverlap = "rawOverlap"



class ImbuError(Exception):
  pass



class ImbuUnableToLoadModelError(ImbuError):
  def __init__(self, exc):
    self.exc = exc



def _loadNetworkConfig(jsonName=None):
  """ Load network config by calculating path relative to this file, and load
  with htmresearch.frameworks.nlp.model_factory.getNetworkConfig()
  """
  if not jsonName:
    raise RuntimeError("Need a config file to build the network model.")

  root = (
    os.path.dirname(
      os.path.dirname(
        os.path.dirname(
          os.path.dirname(
            os.path.realpath(__file__)
          )
        )
      )
    )
  )

  return getNetworkConfig(
    os.path.join(root, "projects/nlp/data/network_configs", jsonName))



class ImbuModels(object):

  defaultSimilarityMetric = ModelSimilarityMetrics.pctOverlapOfInput
  defaultModelType = "CioDocumentFingerprint"
  defaultDataset = "sample_reviews"
  defaultRetina = "en_associative"
  defaultContextSize = 20
  tokenIndexingFactor = 1000

  # Mapping of acceptable model names to names expected by the model factory
  modelMappings = dict((name, name) for name in ("CioWordFingerprint",
                                                 "CioDocumentFingerprint",
                                                 "HTMNetwork",
                                                 "Keywords"))
  modelMappings.update(HTM_sensor_knn="HTMNetwork",
                       HTM_sensor_simple_tp_knn="HTMNetwork",
                       HTM_sensor_tm_knn="HTMNetwork",
                       HTM_sensor_tm_simple_tp_knn="HTMNetwork")

  # Set of classification model types that accept CioEncoder kwargs
  requiresCIOKwargs = {
    ClassificationModelTypes.CioWordFingerprint,
    ClassificationModelTypes.CioDocumentFingerprint,
    ClassificationModelTypes.HTMNetwork,
    ClassificationModelTypes.DocumentFingerPrint
  }

  # Set of classification model types that run document-level training/inference
  documentLevel = {
    ClassificationModelTypes.CioWordFingerprint,
    ClassificationModelTypes.CioDocumentFingerprint,
    ClassificationModelTypes.DocumentFingerPrint
  }


  def __init__(self, dataPath, cacheRoot=None, modelSimilarityMetric=None,
      apiKey=None, retina=None):

    if not dataPath:
      raise RuntimeError("Imbu needs a CSV datafile to run.")

    self.dataPath = dataPath
    self.cacheRoot = cacheRoot
    self.modelSimilarityMetric = (
      modelSimilarityMetric or self.defaultSimilarityMetric
    )
    self.dataDict = self._loadData()
    self.apiKey = apiKey
    self.retina = retina or self.defaultRetina


  def __repr__(self):
    return ("ImbuModels<cacheRoot={cacheRoot}, dataPath={dataPath}, "
            "modelSimilarityMetric={modelSimilarityMetric}, "
            "apiKey={apiKey}, retina={retina}>"
            .format(**self.__dict__))


  def _mapModelName(self, modelName):
    """ Return the model name that is expected by the model factory.
    """
    mappedName = self.modelMappings.get(modelName, None)
    if mappedName is None:
      raise ValueError(
        "'{}' is not an acceptable model name for Imbu".format(modelName))

    return mappedName


  def _defaultModelFactoryKwargs(self):
    """ Default kwargs common to all model types.

    For Imbu to function unsupervised, numLabels is set to 1 in order to use
    unlabeled data for querying and still comply with models' inference logic.
    """
    return dict(
      numLabels=1,
      classifierMetric=self.modelSimilarityMetric,
      textPreprocess=False)


  def _modelFactory(self, modelName, savePath, **kwargs):
    """ Imbu model factory.  Returns a concrete instance of a classification
    model given a model type name and kwargs.

    @param modelName (str)    Must be one of 'CioWordFingerprint',
        'CioDocumentFingerprint', 'HTMNetwork', 'Keywords'.
    """
    kwargs.update(modelDir=savePath, **self._defaultModelFactoryKwargs())

    modelName = self._mapModelName(modelName)

    if getattr(ClassificationModelTypes, modelName) in self.requiresCIOKwargs:
      # Model type requires Cortical.io credentials
      kwargs.update(retina=self.retina, apiKey=self.apiKey)
      # Specify encoder params
      kwargs.update(cacheRoot=self.cacheRoot, retinaScaling=1.0)

    if modelName == "CioWordFingerprint":
      kwargs.update(fingerprintType=EncoderTypes.word)

    elif modelName == "CioDocumentFingerprint":
      kwargs.update(fingerprintType=EncoderTypes.document)

    elif modelName == "HTMNetwork":
      try:
        kwargs.update(
          networkConfig=_loadNetworkConfig(kwargs["networkConfigName"]))
      except Exception as e:
        print "Could not add params to HTMNetwork model config."
        raise e

    elif modelName == "Keywords":
      # k should be > the number of data samples because the Keywords model
      # looks for exact matching tokens, so we want to consider all data
      # samples in the search of k nearest neighbors.
      kwargs.update(k=10 * len(self.dataDict.keys()))

    else:
      raise ValueError("{} is not an acceptable Imbu model.".format(modelName))

    model = createModel(modelName, **kwargs)

    model.verbosity = 0

    return model


  def _initResultsDataStructure(self, modelType):
    """ Initialize a results dict to be populated in formatResults().

    windowSize specifies the number of previous words (inclusive) that each
    score represents.
    """

    # fragmentOrigins is to list the indices that defined the center(s) of the
    # document's fragment(s). If there are no fragments for a given document, this
    # remains an empty list.

    # fragmentContext is the number of tokens the precede and follow the fragment
    # origin for any given fragment.

    resultsDict = {}
    for docId, document in self.dataDict.iteritems():
      if modelType in self.documentLevel:
        # Only one match per document
        scoresArray = [0]
        windowSize = 0
      else:
        scoresArray = [0] * len(document[0].split(" "))
        windowSize = 1
      resultsDict[docId] = {"text": document[0],
                            "scores": scoresArray,
                            "windowSize": windowSize}

    return resultsDict


  def createModel(self, modelName, loadPath, savePath, *modelFactoryArgs,
      **modelFactoryKwargs):
    """ Creates a new model and trains it, or loads a previously trained model
    from specified loadPath.
    """
    # The model name must be an identifier defined in the model factory mapping.
    modelType = getattr(ClassificationModelTypes, self._mapModelName(modelName))




    if loadPath:
      # User has explicitly specified a load path and expects a model to exist
      try:
        model = ClassificationModel.load(loadPath)

      except IOError as exc:
        # Model was not found, user may have specified incorrect path, DO NOT
        # attempt to create a new model and raise an exception
        raise ImbuUnableToLoadModelError(exc)
    else:
      # User has not specified a load path, defer to default case and
      # gracefully create a new model
      try:
        model = ClassificationModel.load(loadPath)
      except IOError as exc:
        model = self._modelFactory(modelName,
                                   savePath,
                                   *modelFactoryArgs,
                                   **modelFactoryKwargs)
        self.train(model, savePath)

    return model


  def _loadData(self):
    """ Load data.
    """
    return readCSV(self.dataPath,
                   numLabels=0) # 0 to train models in unsupervised fashion


  def train(self, model, savePath=None):
    """ Train model, generically assigning category 0 to all documents.
    Document-level models train on each document with the associated ID.
    Word-level models train on each token with an ID that points to its
    corresponding word in the original text. This ID is generated by multiplying
    the document's ID by an indexing factor (e.g. 1000), and then adding the
    mapping index (from the tokenizer); e.g., the tenth token of the third
    document will have ID #2009.
    """
    labels = [0]
    modelType = type(model)
    for seqId, (text, _, _) in enumerate(tqdm(self.dataDict.values())):
      if modelType in self.documentLevel:
        model.trainDocument(text, labels, seqId)
      else:
        # Word-level model, so use token-word mappings
        tokenList, mapping = model.tokenize(text)
        lastTokenIndex = len(tokenList) - 1
        for i, (token, tokenIndex) in enumerate(zip(tokenList, mapping)):
          wordId = seqId * self.tokenIndexingFactor + tokenIndex
          model.trainToken(token,
                           labels,
                           wordId,
                           resetSequence=int(i == lastTokenIndex))

    if savePath:
      self.save(model, savePath)


  @staticmethod
  def query(model, query, returnDetailedResults=True, sortResults=False):
    """ Query classification model.
    """
    return model.inferDocument(query,
                               returnDetailedResults=returnDetailedResults,
                               sortResults=sortResults)


  def save(self, model, savePath=None):
    """ Save classification model.
    """
    model.save(savePath)


  def formatResults(self, modelName, query, distanceArray, idList,
      contextSize=None):
    """ Format results for passing to frontend of Imbu app:
      [
        {
          'scores': [],             # scalar values, length > 0
          'text': 'Hello world!',   # text string
          'windowSize': 10,         # integer >= 0
        },
        ...
      ]

    For specific models we can expect some of the values:
      a. Document-level models -- CioWordFingerprint and CioDocumentFingerprint:
        windowSize = 0
        length of scores lists = 1
      b. Word-level, non-windows models -- Keywords, HTM_sensor_knn:
        windowSize = 1
      c. HTM_sensor_simple_tp_knn:
        windowSize = 10

    Windows correspond to the last token of the window, so a window of length
    10 for index 13 implies the window contains indices 4-13.
    """
    # Consistent with the JS components (search-results.jsx) we tokenize simply
    # on spaces.
    queryLength = float(len(query.split(" ")))
    formattedDistances = (1.0 - distanceArray) * 100

    modelType = (
      getattr(ClassificationModelTypes, self._mapModelName(modelName))
      or self.defaultModelType )

    # Format results - initially each entry represents one document.
    results = self._initResultsDataStructure(modelType)

    # Set fragment context size
    contextSize = contextSize or self.defaultContextSize

    for protoId, dist in zip(idList, formattedDistances):
      if modelType in self.documentLevel:
        results[protoId]["scores"][0] = dist.item()
      else:
        # Get the sampleId from the protoId via the indexing scheme
        wordId = protoId % self.tokenIndexingFactor
        sampleId = (protoId - wordId) / self.tokenIndexingFactor
        results[sampleId]["scores"][wordId] = dist.item()
      if modelName in ("HTM_sensor_simple_tp_knn",
                       "HTM_sensor_tm_simple_tp_knn"):
        # Windows always length 10
        results[sampleId]["windowSize"] = 10

    if modelType in self.documentLevel:
      # Doc-level results remain documents, not fragments of documents
      return [r for r in results.values()]  # TODO: go back to dict type b/c need keys for the doc popups??

    return self._fragmentResults(results, contextSize)


  def _fragmentResults(self, results, contextSize):
    """ Takes results dict (generated in formatResults()) and splits the results
    entries into fragments.

    """
    # resultsFragments = {}
    resultsFragments = []  ## inconsistent return type with the calling method
    for docId, docResult in results.iteritems():
      # Find everywhere there is a max-scoring match. These indices of the doc
      # are "origins" that define the center of fragments.
      maxScore = max(docResult["scores"])
      if maxScore == 0: continue
      fragmentOrigins = [i for i, s in enumerate(docResult["scores"])
                         if s == maxScore]

      # Consistent with the JS components (search-results.jsx) we tokenize
      # simply on spaces.
      docText = docResult["text"].split(" ")
      docLength = len(docText)

      for i, origin in enumerate(fragmentOrigins):
        # A new fragment dict is created for each "origin"
        newDocFragment = copy.deepcopy(docResult)

        # Start/end indices cannot be outside of the document
        startIndex = origin - docResult["windowSize"] - contextSize
        if startIndex <= 0:
          startIndex = 0
        endIndex = origin + contextSize + 1
        if endIndex >= (docLength):
          endIndex = docLength

        # TODO: Do we want to merge fragments that overlap? YES

        # Populate new fragment dict with subsets of the doc's text and scores
        # And add ellipsis to show document continuation
        fragmentText = docText[startIndex:endIndex]
        newDocFragment["scores"] = docResult["scores"][startIndex:endIndex]
        if startIndex > 0:
          fragmentText.insert(0, "...")
          newDocFragment["scores"].insert(0, 0.0)
        if endIndex < docLength:
          fragmentText.append("...")
          newDocFragment["scores"].append(0.0)
        newDocFragment["text"] = string.join(fragmentText)

        # resultsFragments[docId] = newDocFragment
        resultsFragments.append(newDocFragment)

        # If the first fragment contains the rest of the document, only make
        # a fragment from the first origin; add'l fragments (from later origins)
        # will just reflect repeated text.
        if i==0 and endIndex==docLength: break

    return resultsFragments



def startImbu(args):
  """
  Main entry point for Imbu CLI utility to demonstration Imbu functionality.

  @param args (argparse.Namespace) Specifies params for instantiating ImbuModels
      and creating a model.

  @return imbu (ImbuModels) A new Imbu instance.
  @return model (ClassificationModel) A new or loaded NLP model instance.
  """
  imbu = ImbuModels(
    cacheRoot=args.cacheRoot,
    modelSimilarityMetric=args.modelSimilarityMetric,
    dataPath=args.dataPath,
    retina=args.imbuRetinaId,
    apiKey=args.corticalApiKey
  )

  model = imbu.createModel(args.modelName,
                           loadPath=args.loadPath,
                           savePath=args.savePath,
                           networkConfigName=args.networkConfigName
  )

  return imbu, model



def runQueries(imbu, model, modelName):
  """ Use an ImbuModels instance to query the model from the command line.

  @param imbu (ImbuModels) A new Imbu instance.
  @param model (ClassificationModel) A new or loaded NLP model instance.
  @param modelName (str) Model type identifier; one of ImbuModels.modelMappings.
  """
  while True:
    print "Now we query the model for samples (quit with 'q')..."

    query = raw_input("Enter a query: ")

    if query == "q":
      break

    _, unSortedIds, unSortedDistances = imbu.query(model, query)

    results = imbu.formatResults(
      modelName, query, unSortedDistances, unSortedIds)

    pprint.pprint(results)



def getArgs():
  """ Parse the command line options, returned as a dict.
  """
  parser = argparse.ArgumentParser()

  parser.add_argument("--cacheRoot",
                      type=str,
                      help="Root directory in which to cache encodings")
  parser.add_argument("--modelSimilarityMetric",
                      default=ImbuModels.defaultSimilarityMetric,
                      type=str,
                      help="Classifier metric. Note: HTMNetwork model uses "
                           "the metric specified in the network config "
                           "file.")
  parser.add_argument("-d", "--dataPath",
                      type=str,
                      help="Path to data CSV; samples must be in column w/ "
                           "header 'Sample'; see readCSV() for more.")
  parser.add_argument("-m", "--modelName",
                      choices=ImbuModels.modelMappings,
                      default=ImbuModels.defaultModelType,
                      type=str,
                      help="Name of model class.")
  parser.add_argument("-c", "--networkConfigName",
                      default="imbu_sensor_knn.json",
                      type=str,
                      help="Name of JSON specifying the network params. It's "
                           "expected the file is in the data/network_configs/ "
                           "dir.")
  parser.add_argument("--loadPath",
                      default="",
                      type=str,
                      help="Path from which to load a serialized model.")
  parser.add_argument("--savePath",
                      type=str,
                      help=("Directory path for saving the model. This "
                            "directory should only be used to store a saved "
                            "model. If the directory does not exist, it will "
                            "be created automatically and populated with model"
                            " data. A pre-existing directory will only be "
                            "accepted if it contains previously saved model "
                            "data. If such a directory is given, the full "
                            "contents of the directory will be deleted and "
                            "replaced with current"))
  parser.add_argument("--imbuRetinaId",
                      default=os.environ.get("IMBU_RETINA_ID"),
                      type=str)
  parser.add_argument("--corticalApiKey",
                      default=os.environ.get("CORTICAL_API_KEY"),
                      type=str)
  parser.add_argument("--noQueries",
                      default=False,
                      action="store_true",
                      help="Skip command line queries. This flag is used when "
                           "running imbu.py for training models.")

  return parser.parse_args()



if __name__ == "__main__":

  args = getArgs()

  imbu, model = startImbu(args)

  if not args.noQueries:
    runQueries(imbu, model, args.modelName)
