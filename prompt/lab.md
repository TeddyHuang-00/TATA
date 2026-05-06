# Lab-Specific Grading Addendum

## Scope

Apply these guidelines to programming assignments and notebook-based labs.

## Evaluation Principles

### Code and Implementation

- **Prioritize Logic Over Style**: Grade on correctness, efficiency, and understanding demonstrated by the code—not on exact implementation style or formatting preferences.
- **Accept Equivalent Approaches**: If a solution satisfies the rubric intent using a different algorithm, language feature, or paradigm than the reference implementation, evaluate it on its merits.
- **Minimize Boilerplate Penalties**: Don't penalize missing imports, comment blocks, or scaffolding code unless they directly affect functionality or are explicitly required by the rubric.
- **Handle Edge Cases Gracefully**: If a submission handles core logic correctly but misses some edge cases, acknowledge the solid foundation before pointing out incomplete coverage.
- **Allow for Creative Solutions**: The reference implementation is just one way to solve the problem. If a student's code demonstrates a valid alternative approach that meets the learning objectives, recognize it.

### Notebooks and Outputs

- **Evaluate Process and Results Together**: For notebooks, assess both the computational steps shown and the correctness of final outputs (if provided).
- **Contextualize Pre-Processed Content**: If the grading system extracted only key code cells from a notebook, evaluate what is present without over-penalizing missing explanatory cells or boilerplate.
- **Interpret Output Meaningfully**: If output is present (plots, tables, results), use it as evidence of work even if the internal state or code isn't fully visible.

## Handling Incomplete or Ambiguous Submissions

- **Missing Evidence for a Criterion**: If crucial code or output is absent, explain clearly what specific evidence is needed to assess the criterion fully.
- **Partial Implementations**: Credit what is correct and working; note what remains incomplete rather than treating partial work as entirely flawed.
- **Conflicting or Unclear Intent**: When a student's intent is ambiguous, prefer a charitable interpretation that gives credit for demonstrated understanding.

## Feedback Guidance

- **Connect to Learning**: Frame suggestions around what the student is learning (e.g., "This approach works well for small inputs; thinking about scalability...")—not style dogma.
- **Show Examples Where Helpful**: For common mistakes (off-by-one errors, type issues), a brief example of the corrected pattern can be more useful than abstract description.

## Tolerance Guidelines: Cosmetic vs Functional Differences

When comparing student code to the reference, classify every difference into one of three categories:

### COSMETIC (always acceptable — mark highest correctness level)

| Type | Reference | Student (OK) |
|------|-----------|-------------|
| Variable naming | `df = pd.read_csv(...)` | `data = pd.read_csv(...)` |
| Equivalent API | `np.mean(arr)` | `arr.mean()` |
| Equivalent construct | `[x*2 for x in lst]` | `for x in lst: result.append(x*2)` |
| Reorder independent statements | `import numpy\nimport pandas` | `import pandas\nimport numpy` |
| Extra comments | `x = a + b` | `x = a + b  # add values` |
| Minor formatting | `x=5` | `x = 5` |
| Different string style | `"output"` | `'output'` |
| Equivalent method chain | `df.groupby('x').mean().reset_index()` | `df.groupby('x').agg('mean').reset_index()` |

### FUNCTIONAL (potentially affects behavior — review carefully)

| Type | Reference | Student (Review) |
|------|-----------|-----------------|
| Wrong operation | `df.mean()` | `df.sum()` |
| Missing method call | `df.groupby('col').mean()` | `df.groupby('col')` |
| Wrong argument order | `plt.plot(x, y, color='red')` | `plt.plot(y, x, color='red')` |
| Different logic | `if x > 0: return sqrt(x)` | `return sqrt(abs(x))` |
| Incorrect dimension | `arr.reshape(3, 4)` | `arr.reshape(4, 3)` |
| Wrong parameter value | `kmeans = KMeans(n_clusters=3)` | `kmeans = KMeans(n_clusters=5)` |
| Missing required parameter | `pd.read_csv('data.csv')` | `pd.read_csv('data.csv', sep='\\t')` (when comma-separated) |

### MISSING (required element absent)

| Scenario | Rating Impact |
|----------|--------------|
| TODO cell left as `pass` or placeholder | Lowest correctness level |
| Required plot not generated | Lowest or intermediate level |
| Required function not defined | Lowest correctness level |
| Required analysis step skipped | Intermediate level if rest is correct |

### Decision Flow

```
For each criterion:
1. List ALL differences between reference and student
2. Categorize each as COSMETIC, FUNCTIONAL, or MISSING
3. Apply rating:
   - Only COSMETIC → highest correctness level
   - Some FUNCTIONAL/MISSING but core understanding shown → intermediate
   - Fundamentally wrong or empty → lowest
4. In feedback, mention cosmetic differences briefly but don't penalize them
```

**Rule of thumb:** If you could rename a few variables and the code would be identical to the reference, it's COSMETIC and deserves full credit.
