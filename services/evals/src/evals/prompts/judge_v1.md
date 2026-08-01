Score a support answer against its retrieved context and a reference answer.

Score each dimension from 0.0 to 1.0:
- faithfulness: every claim in the answer is supported by the context passages
- answer_relevance: the answer addresses the question that was asked
- context_relevance: the retrieved passages were useful for answering the question

Judge only what is present. Do not reward an answer for outside knowledge that
happens to be correct but is absent from the context.

Question: {question}

Reference answer: {ideal_answer}

Context passages:
{context}

Answer under evaluation: {answer}
