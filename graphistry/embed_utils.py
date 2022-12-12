import logging
import numpy as np
import pandas as pd
from typing import Optional, Union, Callable, List, TYPE_CHECKING, Any, Tuple, TypedDict

from .PlotterBase import Plottable
from .compute.ComputeMixin import ComputeMixin

def lazy_embed_import_dep():
    try:
        import torch
        import torch.nn as nn
        import dgl
        from dgl.dataloading import GraphDataLoader
        import torch.nn.functional as F
        from .networks import HeteroEmbed
        from tqdm import trange
        return True, torch, nn, dgl, GraphDataLoader, HeteroEmbed, F, trange

    except:
        return False, None, None, None, None, None, None, None


if TYPE_CHECKING:
    _, torch, _, _, _, _, _, _ = lazy_embed_import_dep()
    TT = torch.Tensor
    MIXIN_BASE = ComputeMixin
else:
    TT = Any
    MIXIN_BASE = object
    torch = Any

XSymbolic = Optional[Union[List[str], str, pd.DataFrame]]
ProtoSymbolic = Optional[Union[str, Callable[[TT, TT, TT], TT]]]  # type: ignore

logging.StreamHandler.terminator = ""
logger = logging.getLogger(__name__)


def log(msg:str) -> None:
    # setting every logs to WARNING level
    logger.log(msg=msg, level=30)


class EmbedDistScore:
    @staticmethod
    def TransE(h:TT, r:TT, t:TT) -> TT:  # type: ignore
        return (h + r - t).norm(p=1, dim=1)  # type: ignore

    @staticmethod
    def DistMult(h:TT, r:TT, t:TT) -> TT:  # type: ignore
        return (h * r * t).sum(dim=1)  # type: ignore

    @staticmethod
    def RotatE(h:TT, r:TT, t:TT) -> TT:  # type: ignore
        return -(h * r - t).norm(p=1, dim=1)  # type: ignore


class HeterographEmbedModuleMixin(MIXIN_BASE):
    def __init__(self):
        super().__init__()

        self._protocol = {
            "TransE": EmbedDistScore.TransE,
            "DistMult": EmbedDistScore.DistMult,
            "RotatE": EmbedDistScore.RotatE,
        }

        self._node2id: TypedDict = {}
        self._relation2id: TypedDict = {}
        self._id2node: TypedDict = {}
        self._id2relation: TypedDict = {}
        self._relation: str = None
        self._use_feat = False
        self._kg_embed_dim = None
        self._kg_embeddings = None
        
        self._embed_model = None

        self.train_idx = None
        self.test_idx = None

        self._num_nodes = None
        self._train_split = None
        self._eval_flag = None

        self._build_new_embedding_model = None
        self.proto = None
        self._device = "cpu"

    def _preprocess_embedding_data(self, res, train_split:Union[float, int] = 0.8) -> Plottable:
        _, torch, _, _, _, _, F, _ = lazy_embed_import_dep()
        log('Preprocessing embedding data')
        src, dst = res._source, res._destination
        relation = res._relation

        if res._node is not None and res._nodes is not None:
            nodes = res._nodes[self._node]
        elif res._node is None and res._nodes is not None:
            nodes = res._nodes.reset_index(drop=True).reset_index()["index"]
        else:
            res = res.materialize_nodes()
            nodes = res._nodes[res._node]
        
        edges = res._edges
        edges = edges[edges[src].isin(nodes) & edges[dst].isin(nodes)]
        relations = edges[relation].unique()

        # type2id
        res._node2id = {n: idx for idx, n in enumerate(nodes)}
        res._relation2id = {r: idx for idx, r in enumerate(relations)}

        res._id2node = {idx: n for idx, n in enumerate(nodes)}
        res._id2relation = {idx: r for idx, r in enumerate(relations)}

        s, r, t = (
            edges[src].map(res._node2id),
            edges[relation].map(res._relation2id),
            edges[dst].map(res._node2id),
        )
        triplets = torch.from_numpy(pd.concat([s, r, t], axis=1).to_numpy())

        # split idx
        if res.train_idx is None or res._train_split != train_split:
            log(msg="--Splitting data")
            train_size = int(train_split * len(triplets))
            test_size = len(triplets) - train_size
            train_dataset, test_dataset = torch.utils.data.random_split(triplets, [train_size, test_size])
            res.train_idx = train_dataset.indices
            res.test_idx = test_dataset.indices

        res.triplets = triplets
        res._num_nodes, res._num_rels = (len(res._node2id), len(res._relation2id))
        log(
            f"--num_nodes: {res._num_nodes}, num_relationships: {res._num_rels}")
        return res

    def _build_graph(self, res) -> Plottable:
        _, _, _, dgl, _, _, _, _ = lazy_embed_import_dep()
        s, r, t = res.triplets.T

        if res.train_idx is not None:
            g_dgl = dgl.graph(
                (s[res.train_idx], t[res.train_idx]), num_nodes=self._num_nodes
            )
            g_dgl.edata[dgl.ETYPE] = r[res.train_idx]

        else:
            g_dgl = dgl.graph(
                (s, t), num_nodes=self._num_nodes
            )
            g_dgl.edata[dgl.ETYPE] = r

        g_dgl.edata["norm"] = dgl.norm_by_dst(g_dgl).unsqueeze(-1)
        res.g_dgl = g_dgl
        return res


    def _init_model(self, res, batch_size:int, sample_size:int, num_steps:int, device):
        _, _, _, _, GraphDataLoader, HeteroEmbed, _, _ = lazy_embed_import_dep()
        g_iter = SubgraphIterator(res.g_dgl, sample_size, num_steps)
        g_dataloader = GraphDataLoader(
            g_iter, batch_size=batch_size, collate_fn=lambda x: x[0]
        )

        # init model
        model = HeteroEmbed(
            res._num_nodes,
            res._num_rels,
            res._kg_embed_dim,
            proto=res.proto,
            node_features=res._node_features,
            device=device,
        )

        return model, g_dataloader

    def _train_embedding(self, res, epochs:int, batch_size:int, lr:float, sample_size:int, num_steps:int, device) -> Plottable:
        _, torch, nn, _, _, _, _, trange = lazy_embed_import_dep()
        log('Training embedding')
        model, g_dataloader = res._init_model(res, batch_size, sample_size, num_steps, device)
        if hasattr(res, "_embed_model") and not res._build_new_embedding_model:
            model = res._embed_model
            log("--Reusing previous model")

        optimizer = torch.optim.Adam(model.parameters(), lr=lr)
        pbar = trange(epochs, desc=None)
        model.to(device)

        score = 0
        for epoch in pbar:
            model.train()
            for data in g_dataloader:
                g, edges, labels = data

                g = g.to(device)
                edges = edges.to(device)
                labels = labels.to(device)

                emb = model(g)
                loss = model.loss(emb, edges, labels)
                optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                optimizer.step()
                pbar.set_description(
                    f"epoch: {epoch+1}, loss: {loss.item():.4f}, score: {100*score:.4f}%"
                )

            model.eval()
            res._kg_embeddings = model(res.g_dgl.to(device)).detach()
            res._embed_model = model
            if res._eval_flag and self.train_idx is not None:
                score = res._eval(threshold=0.5)
                pbar.set_description(
                    f"epoch: {epoch+1}, loss: {loss.item():.4f}, score: {100*score:.2f}%"
                )

        return res

    @property
    def gcn_node_embeddings(self):
        _, torch, _, _, _, _, _, _ = lazy_embed_import_dep()
        g_dgl = self.g_dgl.to(self._device)
        em = self._embed_model(g_dgl).detach()
        del g_dgl
        torch.cuda.empty_cache()
        return em

    def embed(
        self,
        relation:str,
        proto: ProtoSymbolic = 'DistMult',
        embedding_dim: int = 32,
        use_feat: bool = False,
        X: XSymbolic = None,
        epochs: int = 2,
        batch_size: int = 32,
        train_split: Union[float, int] = 0.8,
        sample_size: int = 1000, 
        num_steps: int = 50,
        lr: float = 1e-2,
        inplace: Optional[bool] = False,
        device: Optional['str'] = "cpu",
        evaluate: bool = True,
        *args,
        **kwargs,
    ) -> Plottable:
        """Embed a graph using a relational graph convolutional network (RGCN),
        and return a new graphistry graph with the embeddings as node
        attributes.


        Parameters
        ----------
        relation : str
            column to use as relation between nodes
        proto : ProtoSymbolic
            metric to use, ['TransE', 'RotateE', 'DistMult'] or provide your own. Defaults to 'DistMult'.
        embedding_dim : int
            relation embedding dimension. defaults to 32
        use_feat : bool
            wether to featurize nodes, if False will produce random embeddings and shape them during training.
            Defaults to True
        X : XSymbolic
            Which columns in the nodes dataframe to featurize. Inherets args from graphistry.featurize().
            Defaults to None.
        epochs : int
            Number of training epochs. Defaults to 2
        batch_size : int
            batch_size. Defaults to 32
        train_split : Union[float, int]
            train percentage, between 0, 1. Defaults to 0.8.
        sample_size : int
            sample size. Defaults to 1000
        num_steps : int
            num_steps. Defaults to 50
        lr : float
            learning rate. Defaults to 0.002
        inplace : Optional[bool]
            inplace
        device : Optional[str]
            accelarator. Defaults to "cpu"
        evaluate : bool
            Whether to evaluate. Defaults to False.

        Returns
        -------
            self : graphistry instance
        """
        #_, torch, nn, dgl, GraphDataLoader, _, F, _ = lazy_embed_import_dep()
        if inplace:
            res = self
        else:
            res = self.bind()
        
        requires_new_model = False
        if res._relation != relation:
            requires_new_model = True
            res._relation = relation
        if res._use_feat != use_feat:
            requires_new_model = True
            res._use_feat = use_feat
        if res._kg_embed_dim != embedding_dim:
            requires_new_model = True
            res._kg_embed_dim = embedding_dim
        res._build_new_embedding_model = requires_new_model
        res._train_split = train_split
        res._eval_flag = evaluate
        res._device = device

        if callable(proto):
            res.proto = proto
        else:
            res.proto = res._protocol[proto]

        if res._use_feat and res._nodes is not None:
            res = res.featurize(kind="nodes", X=X, *args, **kwargs)  # type: ignore

        if not hasattr(res, "triplets") or res._build_new_embedding_model:
            res = res._preprocess_embedding_data(res, train_split=train_split)  # type: ignore
            res = res._build_graph(res)  # type: ignore

        return res._train_embedding(res, epochs, batch_size, lr=lr, sample_size=sample_size, num_steps=num_steps,device=device)


    def predict_links(
        self,
        test_df: pd.DataFrame,
        src:str,
        rel:str,
        threshold:Optional[float] = 0.95, 
        anomalous=False
    ) -> pd.DataFrame:
        """predict links from a test dataframe given src/dst and rel columns

        Parameters
        ----------
        test_df : pd.DataFrame
            dataframe of test data
        src : str
            source or destination column name
        rel : str
            relation column name
        threshold : Optional[float]
            Probability threshold/confidence. Defaults to 0.95.

        Returns
        -------
        pd.DataFrame
            dataframe containing predicted links

        """
        _, torch, _, _, _, _, _, _ = lazy_embed_import_dep()
        pred = "predicted_destination"
        nodes = test_df[src].map(self._node2id)
        relations = test_df[rel].map(self._relation2id)

        all_nodes = self._node2id.values()
        test_df = pd.concat([nodes, relations], axis=1)
        test_df[pred] = [all_nodes] * len(test_df)
        test_df = test_df.explode(pred)
        test_df = test_df[test_df[src] != test_df[pred]]
        score = self._score(
            torch.from_numpy(test_df.to_numpy().astype(np.float32)).to(dtype=torch.long)
        )
        if anomalous:
            result_df = test_df.loc[score.detach().numpy() <= threshold]  # type: ignore
        else:
            result_df = test_df.loc[score.detach().numpy() >= threshold]  # type: ignore
        s, r, d = (
            test_df[src].map(self._id2node),
            test_df[rel].map(self._id2relation),
            test_df[pred].map(self._id2node),
        )
        result_df = pd.concat([s, r, d], axis=1)
        result_df.columns = [src, rel, pred]  # type: ignore
        return result_df

    def predict_links_all(
        self, 
        source,
        relation,
        destination,
        threshold: Optional[float] = 0.95,
        anomalous: Optional[bool] = False,
        retain_old_edges: Optional[bool] = False
    ) -> Plottable:  # type: ignore
        """predict_links over entire graph given a threshold

        Parameters
        ----------
        threshold : Optional[float]
            Probability threshold. Defaults to 0.99.
        retain_old_edges : Optional[bool]
            will include old edges in predicted graph. Defaults to False.
        anomalous : Optional[False]
            will return the edges < threshold or low confidence edges(anomaly).

        Returns
        -------
        Plottable
            graphistry graph containing predicted_edges/[old_edges + predicted_edges]

        """
        _, torch, _, _, _, _, _, _ = lazy_embed_import_dep()
        h_r = pd.DataFrame(self.triplets.numpy())  # type: ignore
        t_r = h_r.copy()
        t_r[[0,1,2]] = t_r[[2,1,0]]

        all_nodes = self._node2id.values()
        all_relations = self._relation2id.values()

        def fetch_triplets_for_inference(source, relation, destination):

            if source is None:
                source = pd.Series(all_nodes)

            if relations is None:
                relations = pd.Series(all_relations)

            if destination is None:
                destination = pd.Series(all_nodes)
            
            source = pd.DataFrame(source.unique(), columns=['source'])
            source['relation'] = [relation.unique()] * source.shape[0]
            source_with_relation = source.explode('relation')
            source_with_relation['destination'] = [destination.unique()] * source_with_relation.shape[0]
            triplets = source_with_relation.explode('destination')

            # removing source == destination
            triplets = triplets[triplets['source'] != triplets['destination']]
            return triplets.drop_duplicates().reset_index(drop=True)

        triplets = fetch_triplets_for_inference(source, relation, destination)
        triplets = triplets.to_numpy().astype(np.int64)

        scores = self._score(triplets)
        if anomalous:
            predicted_links = triplets[scores < threshold]  # type: ignore
        else:
            predicted_links = triplets[scores > threshold]  # type: ignore

        predicted_links = pd.DataFrame(predicted_links, columns=[self._source, self._relation, self._destination])
        existing_links = self._edges[[self._source, self._relation, self._destination]]
        
        if retain_old_edges:
            all_links = pd.concat(
                [existing_links, predicted_links], ignore_index=True
            ).drop_duplicates()
        else:
            all_links = predicted_links

        g_new = self.nodes(self._nodes, self._node)
        g_new = g_new.edges(all_links, self._source, self._destination)
        return g_new

    def _score(self, triplets: Union[np.ndarray, TT]) -> TT:  # type: ignore
        _, torch, _, _, _, _, _, _ = lazy_embed_import_dep()
        emb = self._kg_embeddings.clone().detach()
        if type(triplets) != torch.Tensor:
            triplets = torch.tensor(triplets)
        score = self._embed_model.score(emb, triplets)
        prob = torch.sigmoid(score)
        return prob.detach()

    def _eval(self, threshold: float):
        if self.test_idx is not None:
            triplets = self.triplets[self.test_idx]  # type: ignore
            score = self._score(triplets)
            score = len(score[score >= threshold]) / len(score)  # type: ignore
            return score
        else:
            log("WARNING: train_split must be < 1 for _eval()")


class SubgraphIterator:
    def __init__(self, g, sample_size:int = 3000, num_steps:int = 1000):
        self.num_steps = num_steps
        self.sample_size = sample_size
        self.eids = np.arange(g.num_edges())
        self.g = g
        self.num_nodes = g.num_nodes()

    def __len__(self) -> int:
        return self.num_steps

    def __getitem__(self, i:int):
        _, torch, nn, dgl, GraphDataLoader, _, F, _ = lazy_embed_import_dep()
        eids = torch.from_numpy(np.random.choice(self.eids, self.sample_size))

        src, dst = self.g.find_edges(eids)
        rel = self.g.edata[dgl.ETYPE][eids].numpy()

        triplets = np.stack((src, rel, dst)).T
        samples, labels = SubgraphIterator._sample_neg(
            triplets,
            self.num_nodes,
        )

        src, rel, dst = samples.T  # type: ignore

        # might need to add bidirectional edges
        sub_g = dgl.graph((src, dst), num_nodes=self.num_nodes)
        sub_g.edata[dgl.ETYPE] = rel
        sub_g.edata["norm"] = dgl.norm_by_dst(sub_g).unsqueeze(-1)

        return sub_g, samples, labels

    @staticmethod
    def _sample_neg(triplets:np.ndarray, num_nodes:int) -> Tuple[TT, TT]:  # type: ignore
        _, torch, _, _, _, _, _, _ = lazy_embed_import_dep()
        triplets = torch.tensor(triplets)
        h, r, t = triplets.T
        h_o_t = torch.randint(high=2, size=h.size())

        random_h = torch.randint(high=num_nodes, size=h.size())
        random_t = torch.randint(high=num_nodes, size=h.size())

        neg_h = torch.where(h_o_t == 0, random_h, h)
        neg_t = torch.where(h_o_t == 1, random_t, t)
        neg_triplets = torch.stack((neg_h, r, neg_t), dim=1)

        all_triplets = torch.cat((triplets, neg_triplets), dim=0)
        labels = torch.zeros((all_triplets.size()[0]))
        labels[: triplets.shape[0]] = 1
        return all_triplets, labels
