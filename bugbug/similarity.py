# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import bisect
import random
import re
from collections import defaultdict

import numpy as np
from pyemd import emd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.neighbors import NearestNeighbors

from bugbug import bugzilla, feature_cleanup

OPT_MSG_MISSING = (
    "Optional dependencies are missing, install them with: pip install bugbug[nlp]\n"
)

try:
    import nltk
    import gensim
    from gensim import models, similarities
    from gensim.models import Word2Vec
    from gensim.models.doc2vec import Doc2Vec, TaggedDocument
    from gensim.corpora import Dictionary
    from nltk.corpus import stopwords
    from nltk.stem.porter import PorterStemmer
    from nltk.tokenize import word_tokenize
except ImportError:
    raise ImportError(OPT_MSG_MISSING)

nltk.download("stopwords")

REPORTERS_TO_IGNORE = {"intermittent-bug-filer@mozilla.bugs", "wptsync@mozilla.bugs"}

cleanup_functions = [
    feature_cleanup.responses(),
    feature_cleanup.hex(),
    feature_cleanup.dll(),
    feature_cleanup.fileref(),
    feature_cleanup.url(),
    feature_cleanup.synonyms(),
    feature_cleanup.crash(),
]

# A map from bug ID to its duplicate IDs
duplicates = defaultdict(set)
all_ids = set(
    bug["id"]
    for bug in bugzilla.get_bugs()
    if bug["creator"] not in REPORTERS_TO_IGNORE and "dupeme" not in bug["keywords"]
)

for bug in bugzilla.get_bugs():
    dupes = [entry for entry in bug["duplicates"] if entry in all_ids]
    if bug["dupe_of"] in all_ids:
        dupes.append(bug["dupe_of"])

    duplicates[bug["id"]].update(dupes)
    for dupe in dupes:
        duplicates[dupe].add(bug["id"])


def get_text(bug):
    return "{} {}".format(bug["summary"], bug["comments"][0]["text"])


def text_preprocess(text, join=False):
    for func in cleanup_functions:
        text = func(text)

    text = re.sub("[^a-zA-Z0-9]", " ", text)

    ps = PorterStemmer()
    text = [
        ps.stem(word)
        for word in text.lower().split()
        if word not in set(stopwords.words("english")) and len(word) > 1
    ]
    if join:
        return " ".join(word for word in text)
    return text


class BaseSimilarity:
    def __init__(self):
        pass

    def evaluation(self):
        total_r = 0
        hits_r = 0
        total_p = 0
        hits_p = 0

        for bug in bugzilla.get_bugs():
            if duplicates[bug["id"]]:
                similar_bugs = self.get_similar_bugs(bug)

                # Recall
                for item in duplicates[bug["id"]]:
                    print(duplicates[bug["id"]])
                    print(similar_bugs)
                    total_r += 1
                    if item in similar_bugs:
                        hits_r += 1

                # Precision
                for element in similar_bugs:
                    total_p += 1
                    if element in duplicates[bug["id"]]:
                        hits_p += 1

        print(f"Recall: {hits_r/total_r * 100}%")
        print(f"Precision: {hits_p/total_p * 100}%")


class LSISimilarity(BaseSimilarity):
    def __init__(self):
        self.corpus = []

        for bug in bugzilla.get_bugs():

            textual_features = text_preprocess(get_text(bug))
            self.corpus.append([bug["id"], textual_features])

        # Assigning unique integer ids to all words
        self.dictionary = Dictionary(text for bug_id, text in self.corpus)

        # Conversion to BoW
        corpus_final = [self.dictionary.doc2bow(text) for bug_id, text in self.corpus]

        # Initializing and applying the tfidf transformation model on same corpus,resultant corpus is of same dimensions
        tfidf = models.TfidfModel(corpus_final)
        corpus_tfidf = tfidf[corpus_final]

        # Transform TF-IDF corpus to latent 300-D space via Latent Semantic Indexing
        self.lsi = models.LsiModel(
            corpus_tfidf, id2word=self.dictionary, num_topics=300
        )
        corpus_lsi = self.lsi[corpus_tfidf]

        # Indexing the corpus
        self.index = similarities.Similarity(
            output_prefix="simdata.shdat", corpus=corpus_lsi, num_features=300
        )

    def get_similar_bugs(self, query, k=10):
        query_summary = "{} {}".format(query["summary"], query["comments"][0]["text"])
        query_summary = text_preprocess(query_summary)

        # Transforming the query to latent 300-D space
        vec_bow = self.dictionary.doc2bow(query_summary)
        vec_lsi = self.lsi[vec_bow]

        # Perform a similarity query against the corpus
        sims = self.index[vec_lsi]
        sims = sorted(enumerate(sims), key=lambda item: -item[1])

        # Get IDs of the k most similar bugs
        return [self.corpus[j[0]][0] for j in sims[:k]]


class NeighborsSimilarity(BaseSimilarity):
    def __init__(self, k=10, vectorizer=TfidfVectorizer()):
        self.vectorizer = vectorizer
        self.similarity_calculator = NearestNeighbors(n_neighbors=k)
        text = []
        self.bug_ids = []

        for bug in bugzilla.get_bugs():
            text.append(text_preprocess(get_text(bug), join=True))
            self.bug_ids.append(bug["id"])

        self.vectorizer.fit(text)
        self.similarity_calculator.fit(self.vectorizer.transform(text))

    def get_similar_bugs(self, query):

        processed_query = self.vectorizer.transform([get_text(query)])
        _, indices = self.similarity_calculator.kneighbors(processed_query)

        return [
            self.bug_ids[ind] for ind in indices[0] if self.bug_ids[ind] != query["id"]
        ]


class Word2VecWmdSimilarity(BaseSimilarity):
    def __init__(self):
        self.corpus = []
        self.bug_ids = []
        for bug in bugzilla.get_bugs():
            self.corpus.append(text_preprocess(get_text(bug)))
            self.bug_ids.append(bug["id"])

        indexes = list(range(len(self.corpus)))
        random.shuffle(indexes)
        self.corpus = [self.corpus[idx] for idx in indexes]
        self.bug_ids = [self.bug_ids[idx] for idx in indexes]

        self.w2vmodel = Word2Vec(self.corpus, size=100, min_count=5)
        self.w2vmodel.init_sims(replace=True)

    # word2vec.wmdistance calculates only the euclidean distance. To get the cosine distance,
    # we're using the function with a few subtle changes. We compute the cosine distances
    # in the get_similar_bugs method and use this inside the wmdistance method.
    def wmdistance(self, document1, document2, all_distances, distance_metric="cosine"):
        model = self.w2vmodel
        if len(document1) == 0 or len(document2) == 0:
            print(
                "At least one of the documents had no words that were in the vocabulary. Aborting (returning inf)."
            )
            return float("inf")

        dictionary = gensim.corpora.Dictionary(documents=[document1, document2])
        vocab_len = len(dictionary)

        # Sets for faster look-up.
        docset1 = set(document1)
        docset2 = set(document2)

        distance_matrix = np.zeros((vocab_len, vocab_len), dtype=np.double)

        for i, t1 in dictionary.items():
            for j, t2 in dictionary.items():
                if t1 not in docset1 or t2 not in docset2:
                    continue

                if distance_metric == "euclidean":
                    distance_matrix[i, j] = np.sqrt(
                        np.sum((model.wv[t1] - model.wv[t2]) ** 2)
                    )
                elif distance_metric == "cosine":
                    distance_matrix[i, j] = all_distances[model.wv.vocab[t2].index, i]

        if np.sum(distance_matrix) == 0.0:
            print("The distance matrix is all zeros. Aborting (returning inf).")
            return float("inf")

        def nbow(document):
            d = np.zeros(vocab_len, dtype=np.double)
            nbow = dictionary.doc2bow(document)
            doc_len = len(document)
            for idx, freq in nbow:
                d[idx] = freq / float(doc_len)
            return d

        d1 = nbow(document1)
        d2 = nbow(document2)

        return emd(d1, d2, distance_matrix)

    def get_similar_bugs(self, query):

        words = text_preprocess(get_text(query))
        words = [word for word in words if word in self.w2vmodel.wv.vocab]

        all_distances = np.array(
            1.0
            - np.dot(
                self.w2vmodel.wv.vectors_norm,
                self.w2vmodel.wv.vectors_norm[
                    [self.w2vmodel.wv.vocab[word].index for word in words]
                ].transpose(),
            ),
            dtype=np.double,
        )
        distances = []
        for i in range(len(self.corpus)):
            cleaned_corpus = [
                word for word in self.corpus[i] if word in self.w2vmodel.wv.vocab
            ]
            indexes = [self.w2vmodel.wv.vocab[word].index for word in cleaned_corpus]
            if len(indexes) != 0:
                word_dists = all_distances[indexes]
                rwmd = max(
                    np.sum(np.min(word_dists, axis=0)),
                    np.sum(np.min(word_dists, axis=1)),
                )

                distances.append((self.bug_ids[i], rwmd))

        distances.sort(key=lambda v: v[1])

        confirmed_distances_ids = []
        confirmed_distances = []

        for i, (doc_id, rwmd_distance) in enumerate(distances):

            if (
                len(confirmed_distances) >= 10
                and rwmd_distance > confirmed_distances[10 - 1]
            ):
                break

            doc_words_clean = [
                word
                for word in self.corpus[self.bug_ids.index(doc_id)]
                if word in self.w2vmodel.wv.vocab
            ]
            wmd = self.wmdistance(words, doc_words_clean, all_distances)

            j = bisect.bisect(confirmed_distances, wmd)
            confirmed_distances.insert(j, wmd)
            confirmed_distances_ids.insert(j, doc_id)

        similarities = zip(confirmed_distances_ids, confirmed_distances)

        return [
            similar[0]
            for similar in sorted(similarities, key=lambda v: v[1])[:10]
            if similar[0] != query["id"]
        ]


class Doc2VecSimilarity(BaseSimilarity):
    def __init__(self):
        self.corpus = []
        self.bug_ids = []
        count = 0
        for bug in bugzilla.get_bugs():
            if count % 1000 == 0:
                print(count)
            self.corpus.append(text_preprocess(get_text(bug)))
            self.bug_ids.append(bug["id"])
            count += 1

        indexes = list(range(len(self.corpus)))
        random.shuffle(indexes)
        self.corpus = [self.corpus[idx] for idx in indexes]
        self.bug_ids = [self.bug_ids[idx] for idx in indexes]

        tagged_corpus = [TaggedDocument(words=d, tags=[str(i)]) for i, d in zip(self.bug_ids, self.corpus)]

        self.d2vmodel = Doc2Vec(vector_size=300, min_count=2, epochs=50)
        self.d2vmodel.build_vocab(tagged_corpus)

    def get_similar_bugs(self, query):
        words = text_preprocess(get_text(query))
        vector = self.d2vmodel.infer_vector(words)
        sims = self.d2vmodel.docvecs.most_similar([vector], topn=10)
        print(sims)
        return [int(d[0]) for d in sims]
