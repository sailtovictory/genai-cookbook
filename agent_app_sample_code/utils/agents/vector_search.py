import mlflow
from mlflow.entities import Document
from mlflow.models.resources import (
    DatabricksVectorSearchIndex,
    DatabricksServingEndpoint,
)

import json
from typing import Literal, Any, Dict, List, Union
from pydantic import BaseModel, model_validator
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.vectorsearch import VectorIndexType
from databricks.sdk.errors import ResourceDoesNotExist
from utils.agents.tools import Tool

FilterDict = Dict[str, Union[str, int, float, List[Union[str, int, float]]]]


class VectorSearchSchema(BaseModel):
    """Configuration for the schema used in the retriever's response.

    This class defines the schema configuration for how the vector search retriever
    structures and returns results.

    Args:
        primary_key: The column name in the retriever's response referred to the unique key.
            If using Databricks vector search with delta sync, this should be the column
            of the delta table that acts as the primary key.
        chunk_text: The column name in the retriever's response that contains the
            returned chunk.
        document_uri: The template of the chunk returned by the retriever - used to format
            the chunk for presentation to the LLM & to display chunk's from the same
            document_uri together in Agent Evaluation Review App.
        additional_metadata_columns: Additional metadata columns to present to the LLM.
        filterable_columns: List of columns that can be used as filters by the LLM.

    Returns:
        VectorSearchSchema: A configured schema object for the vector search retriever.
    """

    _primary_key: str | None = None
    """The column name in the retriever's response referred to the unique key.
    If using Databricks vector search with delta sync, this should be the column
    of the delta table that acts as the primary key, and will be set by reading the index's metadata."""

    chunk_text: str
    """The column name in the retriever's response that contains the returned chunk."""

    document_uri: str
    """The template of the chunk returned by the retriever - used to format
    the chunk for presentation to the LLM & to display chunk's from the same 
    document_uri together in Agent Evaluation Review App."""

    additional_metadata_columns: List[str] = []
    """Additional metadata columns to present to the LLM."""

    @property
    def all_columns(self) -> List[str]:
        cols = [
            self.primary_key,
            self.chunk_text,
            self.document_uri,
        ] + self.additional_metadata_columns
        # de-duplicate
        return list(set(cols))

    @property
    def primary_key(self) -> str:
        """The primary key field, which must be set by VectorSearchRetrieverConfig"""
        if self._primary_key is None:
            raise ValueError("primary_key must be set by VectorSearchRetrieverConfig")
        return self._primary_key


class VectorSearchParameters(BaseModel):
    """Configuration for the input schema (parameters) used in the retriever.

    This class defines the configuration parameters for how the vector search retriever
    performs searches and returns results.

    Args:
        num_results: The number of chunks to return for each query. For example,
            setting this to 5 will return the top 5 most relevant search results.
        query_type: The type of search to use - either 'ann' for semantic similarity
            using embeddings only, or 'hybrid' which combines keyword and semantic
            similarity search.

    Returns:
        VectorSearchParameters: A configured parameters object for the vector search retriever.
    """

    num_results: int = 5
    """The number of chunks to return for each query."""

    query_type: Literal["ann", "hybrid"] = "ann"
    """The type of search to use - either 'ann' for semantic similarity using embeddings only,
    or 'hybrid' which combines keyword and semantic similarity search."""


class VectorSearchRetrieverTool(Tool):
    """Configuration for a Databricks Vector Search retriever.

    This class defines the configuration for a Vector Search retriever that can be used
    either deterministically in a fixed RAG chain or as a tool.

    Args:
        vector_search_index: Unity Catalog location of the Vector Search index.
            Example: catalog.schema.vector_index.
        vector_search_schema: Schema configuration for the retriever.
        doc_similarity_threshold: Threshold (0-1) for the retrieved document's similarity score. Used
            to exclude dissimilar results. Increase if retriever returns irrelevant content.
        vector_search_parameters: Parameters passed to index.similarity_search(...).
            See https://docs.databricks.com/en/generative-ai/create-query-vector-search.html#query-a-vector-search-endpoint for details.
        retriever_query_parameter_prompt: Description of the query parameter for the retriever.

    Returns:
        VectorSearchRetrieverConfig: A configured retriever config object.
    """

    vector_search_index: str
    """Unity Catalog location of the Vector Search index.
    Example: catalog.schema.vector_index."""

    filterable_columns: List[str] = []
    """List of columns that can be used as filters by the LLM.  Columns will be validated against the source table & metadata about each column loaded from the Unity Catalog to improve the LLM's ability to filter."""

    vector_search_schema: VectorSearchSchema
    """Schema configuration for the retriever."""

    doc_similarity_threshold: float = 0.0
    """Threshold (0-1) for the retrieved document's similarity score.
    Used to exclude dissimilar results. Increase if retriever returns irrelevant content."""

    vector_search_parameters: VectorSearchParameters = VectorSearchParameters()
    """Parameters passed to index.similarity_search(...).
    See https://docs.databricks.com/en/generative-ai/create-query-vector-search.html#query-a-vector-search-endpoint for details."""

    retriever_query_parameter_prompt: str = "query to look up in retriever"
    retriever_filter_parameter_prompt: str = (
        "optional filters to apply to the search. An array of objects, each specifying a field name and the filters to apply to that field."
    )

    name: str
    description: str

    def __init__(self, **data):
        """Initialize the WorkspaceClient and set the MLflow retriever schema."""
        super().__init__(**data)
        mlflow.models.set_retriever_schema(
            name=self.vector_search_index,
            primary_key=self.vector_search_schema.primary_key,
            text_column=self.vector_search_schema.chunk_text,
            doc_uri=self.vector_search_schema.document_uri,
        )

    def _validate_columns_exist(
        self, columns: List[str], source_table: str, table_columns: set, context: str
    ) -> None:
        """Helper method to validate that columns exist in the source table.

        Args:
            columns: List of columns to validate
            source_table: Name of the source table
            table_columns: Set of available columns in the table
            context: Context string for error message (e.g. "filterable columns", "chunk_text")
        """
        for col in columns:
            if col not in table_columns:
                raise ValueError(
                    f"Column '{col}' specified in {context} not found in source table {source_table}. "
                    f"Available columns: {', '.join(sorted(table_columns))}"
                )

    def _get_index_and_table_info(self):
        """Helper method to get index and table information."""
        w = WorkspaceClient()
        index_info = w.vector_search_indexes.get_index(self.vector_search_index)

        if index_info.index_type != VectorIndexType.DELTA_SYNC:
            raise ValueError(
                f"Unsupported index type: {index_info.index_type}. Only DELTA_SYNC is supported."
            )

        source_table = index_info.delta_sync_index_spec.source_table
        table_info = w.tables.get(source_table)
        table_columns = {col.name for col in table_info.columns}

        return index_info, source_table, table_info, table_columns

    def _check_if_index_exists(self):
        w = WorkspaceClient()
        try:
            index_info = w.vector_search_indexes.get_index(self.vector_search_index)
            return index_info is not None
        except ResourceDoesNotExist as e:
            return False

    @property
    def filterable_columns_descriptions_for_llm(self) -> str:
        """Returns a formatted description of all filterable columns for use in prompts."""
        try:
            # Get index and table info using shared method
            _, _, table_info, _ = self._get_index_and_table_info()

            # Create mapping of column name to description and type
            column_info = {
                col.name: (col.type_text, col.comment if col.comment else None)
                for col in table_info.columns
            }
            # print(column_info)

            # Build descriptions list
            descriptions = []
            for col in self.filterable_columns:
                type_text, desc = column_info.get(col, (None, None))
                formatted_desc = f"(`{col}`, {type_text}" + (
                    f", '{desc}'" + ")" if desc else ""
                )
                descriptions.append(formatted_desc)
            return ", ".join(descriptions)

        except Exception as e:
            # Fallback to simple formatting if there's any error
            return ", ".join(str(col) for col in self.filterable_columns)

    @model_validator(mode="after")
    def validate_index_and_columns(self):
        """Validates the index exists and all columns after the model is fully initialized"""

        # Check that index exists
        if not self._check_if_index_exists():
            raise ValueError(
                f"Vector search index {self.vector_search_index} does not exist."
            )

        index_info, source_table, _, table_columns = self._get_index_and_table_info()

        # Validate filterable columns
        self._validate_columns_exist(
            self.filterable_columns, source_table, table_columns, "filterable_columns"
        )

        # Set primary key from index if not already set
        if not self.vector_search_schema._primary_key:
            if index_info.primary_key:
                self.vector_search_schema._primary_key = index_info.primary_key
            else:
                raise ValueError(
                    f"Could not find primary key in index {self.vector_search_index}"
                )

        # Validate all configured schema columns exist in source table
        columns_to_validate = [
            (self.vector_search_schema.chunk_text, "chunk_text"),
            (self.vector_search_schema.document_uri, "document_uri"),
        ]

        if self.vector_search_schema.additional_metadata_columns:
            for field in self.vector_search_schema.additional_metadata_columns:
                columns_to_validate.append((field, "additional_metadata_columns"))

        for column, context in columns_to_validate:
            self._validate_columns_exist([column], source_table, table_columns, context)

        return self

    @model_validator(mode="after")
    def validate_threshold(self):
        if not 0 <= self.doc_similarity_threshold <= 1:
            raise ValueError("doc_similarity_threshold must be between 0 and 1")
        return self

    def _get_parameters_schema(self) -> dict:
        schema = {
            "properties": {
                "query": {
                    # "default": None,
                    "description": self.retriever_query_parameter_prompt,
                    "type": "string",
                },
                "type": "object",
                "required": ["query"],
                "additionalProperties": False,
            }
        }

        if self.filterable_columns:
            schema["properties"]["filters"] = {
                # "default": None,
                "description": self.retriever_filter_parameter_prompt,
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "enum": self.filterable_columns,
                            "description": "The fields to apply the filter to.  Can use any of the following as filters, where each is (`field_name`, field_type, 'field_description'): "
                            + self.filterable_columns_descriptions_for_llm
                            + "For string fields, only use LIKE filter; for numeric fields, either provide a number to achieve == or use <, <=, >, >= filters; for array fields, either provide an array of 1+ values to achieve IN or use NOT to exclude.",
                        },
                        "filter": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "number"},
                                {
                                    "type": "array",
                                    "items": {
                                        "anyOf": [
                                            {"type": "string"},
                                            {"type": "number"},
                                        ]
                                    },
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "<": {"type": "number"},
                                        "<=": {"type": "number"},
                                        ">": {"type": "number"},
                                        ">=": {"type": "number"},
                                        "LIKE": {"type": "string"},
                                        "NOT": {
                                            "anyOf": [
                                                {"type": "string"},
                                                {"type": "number"},
                                            ]
                                        },
                                    },
                                    "additionalProperties": False,
                                    "minProperties": 1,
                                    "maxProperties": 1,
                                },
                            ]
                        },
                    },
                    "required": ["field", "filter"],
                    "additionalProperties": False,
                },
            }

        return schema

    @mlflow.trace(span_type="RETRIEVER", name="vector_search_retriever")
    def __call__(self, query: str, filters: Dict[Any, Any] = None) -> List[Document]:
        """
        Performs vector search to retrieve relevant chunks.

        Args:
            query: Search query.
            filters: Optional filters to apply to the search. Should follow the LLM-generated filter pattern of a list of field/filter pairs that will be converted to Databricks Vector Search filter format.

        Returns:
            List of retrieved Documents.
        """
        span = mlflow.get_current_active_span()
        span.set_attributes({"vector_search_index": self.vector_search_index})

        w = WorkspaceClient()

        traced_search = mlflow.trace(
            w.vector_search_indexes.query_index,
            name="_workspace_client.vector_search_indexes.query_index",
            span_type="FUNCTION",
        )

        # Parse filters written by the LLM into Vector Search compatible format
        vs_filters = json.dumps(self.parse_filters(filters)) if filters else None

        results = traced_search(
            index_name=self.vector_search_index,
            query_text=query,
            filters_json=vs_filters,
            columns=self.vector_search_schema.all_columns,
            **self.vector_search_parameters.model_dump(exclude_none=True),
        )

        # We turn the config into a dict and pass it here
        return self.convert_vector_search_to_documents(
            results.as_dict(), self.doc_similarity_threshold
        )

    @mlflow.trace(span_type="PARSER")
    def convert_vector_search_to_documents(
        self, vs_results, vector_search_threshold
    ) -> List[Document]:
        column_names = []
        for column in vs_results["manifest"]["columns"]:
            column_names.append(column)

        docs = []
        if vs_results["result"]["row_count"] > 0:
            for item in vs_results["result"]["data_array"]:
                metadata = {}
                score = item[-1]
                if score >= vector_search_threshold:
                    metadata["similarity_score"] = score
                    for i, field in enumerate(item[0:-1]):
                        metadata[column_names[i]["name"]] = field
                    # put contents of the chunk into page_content
                    page_content = metadata[self.vector_search_schema.chunk_text]
                    del metadata[self.vector_search_schema.chunk_text]

                    # put the primary key into id
                    id = metadata[self.vector_search_schema.primary_key]
                    del metadata[self.vector_search_schema.primary_key]

                    doc = Document(page_content=page_content, metadata=metadata, id=id)
                    docs.append(doc)

        return docs

    @mlflow.trace(span_type="PARSER")
    def parse_filters(self, filters: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Parse input filters into Vector Search compatible format.

        Args:
            filters: List of input filters in the new format.

        Returns:
            Filters in Vector Search compatible format.
        """
        vs_filters = {}
        for filter_item in filters:
            key = filter_item["field"]
            value = filter_item["filter"]

            if isinstance(value, list):
                vs_filters[key] = {"OR": value}
            elif isinstance(value, dict):
                operator, operand = next(iter(value.items()))
                if operator in ["<", "<=", ">", ">="]:
                    vs_filters[f"{key} {operator}"] = operand
                elif operator.upper() == "LIKE":
                    vs_filters[f"{key} LIKE"] = operand
                elif operator.upper() == "NOT":
                    vs_filters[f"{key} !="] = operand
            else:
                vs_filters[key] = value
        return vs_filters

    def get_resource_dependencies(self):
        dependencies = [
            DatabricksVectorSearchIndex(index_name=self.vector_search_index)
        ]

        # Get the embedding model endpoint
        index_info, _, _, _ = self._get_index_and_table_info()
        if index_info.index_type == VectorIndexType.DELTA_SYNC:
            # Only works for DELTA_SYNC indexes
            for (
                embedding_source_col
            ) in index_info.delta_sync_index_spec.embedding_source_columns:
                endpoint_name = embedding_source_col.embedding_model_endpoint_name
                if endpoint_name is not None:
                    dependencies.append(
                        DatabricksServingEndpoint(endpoint_name=endpoint_name),
                    )
                else:
                    raise ValueError(
                        f"Could not identify the embedding model endpoint resource for {self.vector_search_index}.  Please manually add the embedding model endpoint to `databricks_resources`."
                    )
        return dependencies
